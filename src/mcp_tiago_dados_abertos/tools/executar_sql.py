"""executar_sql — execucao SQL DuckDB no S3."""

import re
from typing import Optional

from mcp_tiago_dados_abertos.execucao.db import DUCKDB_OK

# Dica de TRY_CAST APENAS para Binder Error de agregacao sobre VARCHAR
# (Parser/Conversion Error com texto parecido NAO ganham a dica: induziria
# autocorrecao errada).
_VARCHAR_AGG_ERROR = re.compile(
    r"Binder Error.*?(?:(?:sum|avg)\(VARCHAR\)|No function matches[^\n]*VARCHAR)",
    re.IGNORECASE | re.DOTALL,
)


def _hint_varchar_error(error_msg: str) -> str:
    """Anexa dica quando o erro e Binder de agregacao sobre VARCHAR."""
    if _VARCHAR_AGG_ERROR.search(error_msg):
        return f"{error_msg}\nDica: a coluna e texto no parquet -- use TRY_CAST(coluna AS DOUBLE)"
    return error_msg


def _hint_timeout_particao(error_msg: str, sql: str, catalog: dict) -> str:
    """Timeout: diz QUAL filtro resolve. O contrato sabe a particao (prune_partition=year)
    e a coluna temporal — filtrar por ano nela ativa a poda de arquivos no S3."""
    if "tempo limite" not in error_msg:
        return error_msg
    # Fonte de um-arquivo-por-periodo: a janela virou uma LISTA de arquivos e ler N deles por
    # HTTP e o que estourou o tempo. Dizer o numero e mais util que "refine os filtros".
    n_arquivos = len(re.findall(r"'https?://[^']+'", sql or ""))
    if n_arquivos >= 3:
        error_msg += (f" Dica: esta consulta le {n_arquivos} arquivos remotos (a fonte publica um "
                      "arquivo por periodo) — reduza a janela para menos meses.")
    try:
        from mcp_tiago_dados_abertos.resposta.provenance import datasets_do_sql

        dicas = []
        for meta in datasets_do_sql(sql, catalog or {}):
            if (meta or {}).get("prune_partition") != "year":
                continue
            temporal = (((meta.get("semantics") or {}).get("row_grain") or {}).get("temporal_column"))
            if temporal:
                dicas.append(f"`{meta.get('name')}`: filtre por ANO em `{temporal}` "
                             f"(ex.: EXTRACT(YEAR FROM TRY_CAST({temporal} AS DATE)) = 2025) "
                             "— ativa a poda de particao")
        if dicas:
            return error_msg + " Dica: " + "; ".join(dicas)
    except Exception:
        pass
    return error_msg


async def executar_sql(
    sql_query: str, limit: int = 100, offset: int = 0, catalog: Optional[dict] = None
) -> str:
    """Executa SQL DuckDB no S3 do setor eletrico com paginacao.

    IMPORTANTE: Antes de gerar SQL, use descrever_dataset(nome_dataset) para obter os nomes
    exatos das colunas, tipos e instrucoes de TRY_CAST. Nunca chute nomes de colunas.
    Verifique a cobertura temporal no contrato antes de filtrar por datas.

    Args:
        sql_query: SQL DuckDB valido (apenas SELECT).
        limit: Max linhas por pagina (default 100).
        offset: Inicio da pagina.

    Returns:
        Resultado da query ou mensagem de erro.
    """
    if not DUCKDB_OK:
        return "[TIAGO Dados Abertos] DuckDB indisponivel."

    from mcp_tiago_dados_abertos.catalogo.contracts import load_all_contracts

    # Catalogo UMA vez por chamada: o servidor passa o que ja carregou; sem ele,
    # carrega aqui. Antes eram tres cargas por consulta (78 YAML parseados 3x).
    if catalog is None:
        catalog = load_all_contracts()
    from mcp_tiago_dados_abertos.execucao.db import executar_sql_raw

    # Usa executar_sql_raw para ter acesso a columns/rows/truncated
    sucesso, resultado, columns, rows, truncated = await executar_sql_raw(
        sql_query, limit=limit, offset=offset
    )

    if not sucesso:
        resultado = _hint_varchar_error(resultado)
        resultado = _hint_timeout_particao(resultado, sql_query, catalog)
        return f"[ERRO] {resultado}."

    # result-schema: tipagem por contrato+AST de CADA coluna do resultado (papel, unidade com
    # tier de prova, aditividade, rota pattern/adhoc). E o que o modelo precisa para citar
    # unidade e decidir se pode somar; a tabela ja traz os valores.
    from mcp_tiago_dados_abertos.resposta.result_schema import bloco_result_schema, result_schema_do_sql

    rs = result_schema_do_sql(sql_query, columns, catalog, rows=rows)
    return resultado + bloco_result_schema(rs)
