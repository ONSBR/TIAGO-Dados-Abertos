"""executar_sql — execucao SQL DuckDB no S3."""

import json
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

# Regex para extrair coluna-FONTE de agregador SUM/AVG no SQL
# Descarta expressões compostas ilegíveis (só pega coluna simples)
_AGG_COL_RE = re.compile(
    r"\b(?:SUM|AVG)\s*\(\s*(?:TRY_CAST\s*\(\s*)?([a-zA-Z_][a-zA-Z0-9_]*)\s*(?:AS\s+\w+\s*\))?\s*\)",
    re.IGNORECASE,
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


def _extract_aggregated_columns(sql: str) -> list[str]:
    """Extrai nomes das colunas-fonte de SUM/AVG no SQL.

    Descarta expressões compostas — só captura colunas simples.
    Ex: SUM(val_gerhidraulica) → ['val_gerhidraulica']
        SUM(TRY_CAST(val AS DOUBLE)) → ['val']
        SUM(a + b) → [] (expressão composta, não captura)
    """
    out = []
    for m in _AGG_COL_RE.finditer(sql):
        # POS-VALIDACAO: o agregador deve ser o SELECT-item
        # INTEIRO — apos o ')' so alias e virgula/FROM. SUM(val)/1000 ou
        # SUM(val)*24 transformam o VALOR; rotular com unit_native mentiria.
        tail = sql[m.end():]
        import re as _re
        if _re.match(r"\s*(?:AS\s+\w+)?\s*(?:,|FROM[\s(])", tail, _re.IGNORECASE):
            out.append(m.group(1))
    return out


def _find_metric_for_sql(sql: str, meta: dict) -> Optional[dict]:
    """Encontra a métrica do contrato que casa com as colunas agregadas no SQL.

    Retorna a métrica se EXATAMENTE 1 casar, None se 0 ou 2+.
    Casa por:
    - metric.columns contém alguma das colunas agregadas, OU
    - metric.id ou metric.label casa com alias no SQL
    """
    if not meta:
        return None

    semantics = meta.get("semantics") or {}
    metrics = semantics.get("metrics") or []
    if not metrics:
        return None

    agg_cols = _extract_aggregated_columns(sql)
    if not agg_cols:
        return None

    agg_cols_lower = {c.lower() for c in agg_cols}

    matched: list[dict] = []
    for m in metrics:
        if not isinstance(m, dict):
            continue
        # Casa por columns da métrica
        metric_cols = m.get("columns") or []
        metric_cols_lower = {str(c).lower() for c in metric_cols if c}
        if metric_cols_lower & agg_cols_lower:
            matched.append(m)
        # (fallback por id/label REMOVIDO — substring em SQL inteiro casava metrica
        #  errada; colunas-fonte sao a unica ancora confiavel.)

    # EXATAMENTE 1 → retorna; 0 ou 2+ → omite (ambíguo)
    if len(matched) == 1:
        return matched[0]
    return None


def _get_unit_from_metric(metric: dict, sql: str) -> Optional[str]:
    """Unidade p/ o caminho MANUAL: SEMPRE `unit_native` da metrica.

    NUNCA a unit de um modo do contrato: os modos embutem CONVERSAO no SQL deles
    (ex.: total_energy = SUM(val*24/1000) -> GWh); um SUM manual sobre a coluna
    CRUA nao fez essa conversao — rotula-lo com a unit do modo e o erro classico
    de unidade (ex.: SUM(val_cargaenergiamwmed) cru rotulado GWh).
    unit_native descreve a coluna crua — a unica rotulagem honesta aqui.
    """
    if not metric:
        return None
    return metric.get("unit_native") or None


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

    from mcp_tiago_dados_abertos.catalogo.catalog import load_all_contracts

    # Catalogo UMA vez por chamada: o servidor passa o que ja carregou; sem ele,
    # carrega aqui. Antes eram tres cargas por consulta (78 YAML parseados 3x).
    if catalog is None:
        catalog = load_all_contracts()
    from mcp_tiago_dados_abertos.execucao.db import executar_sql_raw
    from mcp_tiago_dados_abertos.resposta.provenance import datasets_do_sql

    # Usa executar_sql_raw para ter acesso a columns/rows/truncated
    sucesso, resultado, columns, rows, truncated = await executar_sql_raw(
        sql_query, limit=limit, offset=offset
    )

    if not sucesso:
        resultado = _hint_varchar_error(resultado)
        resultado = _hint_timeout_particao(resultado, sql_query, catalog)
        return f"[ERRO] {resultado}."

    # ── computed_facts para o caminho manual (executar_sql) ──────────────────
    # Tenta emitir o bloco computed-facts se:
    # 1. EXATAMENTE 1 dataset no SQL
    # 2. EXATAMENTE 1 métrica casada
    # 3. summable=True para essa métrica + esse SQL
    # 4. Não truncado
    facts = None
    try:
        metas = datasets_do_sql(sql_query, catalog)

        # Gate: EXATAMENTE 1 dataset
        if len(metas) == 1:
            meta = metas[0]
            metric = _find_metric_for_sql(sql_query, meta)

            # Gate: métrica encontrada
            if metric:
                from mcp_tiago_dados_abertos.motor.semantics_engine import metric_summable_for_sql

                is_summable = metric_summable_for_sql(metric, sql_query)
                unit = _get_unit_from_metric(metric, sql_query)

                # Emite computed_facts se tiver unit e não truncado
                if unit and columns and rows:
                    from mcp_tiago_dados_abertos.resposta.computed_facts import compute_facts

                    # Cria um plano mock com as propriedades necessárias
                    class MockPlan:
                        pass
                    plano = MockPlan()
                    plano.unit = unit
                    plano.mode = ""
                    plano.summable = is_summable

                    facts = compute_facts(
                        columns, rows, meta, plano,
                        truncated=truncated,
                        executed_sql=sql_query
                    )
    except Exception:
        # Omission-safe: qualquer erro → não emite o bloco
        facts = None

    # ── by_rows: participacao/rank por LINHA, por medida — SEM plano/unidade ─────
    # Generaliza o footer do db (1 medida) e o by_dimension (1 dimensao): dispara
    # no corpus inteiro (read_csv HTTPS incluso) e em tabelas codigo+nome. E o
    # fato que faltou em 'CFURH por subsistema' (01/09): sem ele o LLM inventou %.
    # Rodape textual so com >=2 medidas (com 1, o footer do db ja esta em `resultado`).
    # Revisao: participacao SO em medida ADITIVA provada pelo contrato
    # (lineage AST do SQL: SUM sobre metrica de fluxo, ou COUNT) e rollups do
    # contrato excluidos (has_total_row/exclude_in_aggregation) — nunca "fato"
    # de share de preco nem SIN somado com subsistemas. Motivos de abstencao
    # vao para a telemetria (evt=facts_by_rows).
    rodape = ""
    motivos: dict = {}
    # result-schema ANTES do by_rows: e a fonte UNICA de aditividade por coluna
    # (contrato + AST: modo total_energy AST-equivalente sobre potencia e aditivo; a regra
    # da coluna crua, sozinha, dizia que nao — as duas se contradiziam).
    from mcp_tiago_dados_abertos.resposta.result_schema import (
        bloco_result_schema,
        com_fatos_tipados,
        result_schema_do_sql,
    )

    rs = result_schema_do_sql(sql_query, columns, catalog, rows=rows)
    try:
        from mcp_tiago_dados_abertos.infra.telemetry import evento
        from mcp_tiago_dados_abertos.resposta.computed_facts import facts_by_rows_com_motivo
        from mcp_tiago_dados_abertos.resposta.lineage import rotulos_rollup
        from mcp_tiago_dados_abertos.resposta.provenance import rodape_participacao_multi

        metas_br = datasets_do_sql(sql_query, catalog)
        br, motivos = facts_by_rows_com_motivo(
            columns, rows, schema=rs, truncated=truncated,
            exclude_labels=rotulos_rollup(metas_br),
        )
        if motivos:
            evento(evt="facts_by_rows", emitido=bool(br), motivos=motivos)
        if br:
            facts = {**(facts or {}), **br}
            rodape = rodape_participacao_multi(br["by_rows"])
    except Exception:
        pass

    # ── result-schema: tipagem por contrato+AST de CADA coluna do resultado ──
    # (papel, unidade com tier de prova, aditividade, rota pattern/adhoc). Fonte tipada
    # para o front (substitui regex de cabecalho) e para o LLM (unidade/rotulo de coluna).
    schema_block = bloco_result_schema(rs)
    # + query_id, `facts` tipados (refs f1..fn), `abstentions` (reason_code) e `coverage`
    facts = com_fatos_tipados(facts, rs, truncated=truncated, motivos=motivos)
    computed_block = f"\n\n```computed-facts\n{json.dumps(facts, ensure_ascii=False)}\n```" if facts else ""
    return resultado + rodape + schema_block + computed_block
