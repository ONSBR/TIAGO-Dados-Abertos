"""MCP TIAGO Dados Abertos — Operador Nacional do Sistema Eletrico (ONS).

Entry point unificado: registra tools, carrega catalogo, inicia servidor.
Startup lazy: server inicia imediatamente, contratos carregam na primeira chamada.
Apenas datasets ONS (parquet S3 via DuckDB).
"""

import io
import logging
import sys
import threading

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

# Logging SEMPRE em stderr — stdout e o canal JSON-RPC no modo stdio.
logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from mcp_tiago_dados_abertos.infra.config import HOST, PORT, VERSION
from mcp_tiago_dados_abertos.motor.context import temporal_footer

# ── Lazy catalog loading (background thread) ─────────────────────────────────

CATALOG = {}
_catalog_lock = threading.Lock()
_catalog_loaded = False


def _ensure_catalog():
    """Carrega contratos e indice semantico (thread-safe). Bloqueia se ainda carregando."""
    global CATALOG, _catalog_loaded
    if _catalog_loaded:
        return
    with _catalog_lock:
        if _catalog_loaded:
            return
        from mcp_tiago_dados_abertos.catalogo.catalog import load_all_contracts
        from mcp_tiago_dados_abertos.catalogo.retrieval import build_index

        CATALOG.update(load_all_contracts())
        build_index(CATALOG)
        from mcp_tiago_dados_abertos.execucao.db import DUCKDB_OK  # noqa: F401

        _catalog_loaded = True


def _background_warmup():
    _ensure_catalog()


_warmup_thread = threading.Thread(target=_background_warmup, daemon=True)
_warmup_thread.start()


# ── FastMCP instance ──────────────────────────────────────────────────────────

mcp = FastMCP("MCP TIAGO Dados Abertos - ONS")

# ── Tools ─────────────────────────────────────────────────────────────────────

from mcp_tiago_dados_abertos.infra.toolaudit import log_tool_call
from mcp_tiago_dados_abertos.tools.buscar import buscar_dataset as _buscar
from mcp_tiago_dados_abertos.tools.descrever_dataset import descrever_dataset as _descrever_dataset
from mcp_tiago_dados_abertos.tools.executar_sql import executar_sql as _executar_sql
from mcp_tiago_dados_abertos.tools.listar_datasets import listar_datasets as _listar


def _append_temporal_footer(text: str) -> str:
    """Anexa ancora temporal a toda resposta de tool."""
    return f"{str(text).rstrip()}\n\n{temporal_footer()}"


@mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True, "destructiveHint": False, "openWorldHint": False})
@log_tool_call
async def buscar_dataset(tema: str, orgao: str | None = None) -> str:
    """Filtra o catalogo por termo: ate 10 datasets dos dados abertos do ONS ranqueados por
    palavra-chave e TF-IDF sobre os contratos.

    Complemento de listar_datasets, nao a entrada padrao. O caminho normal e ler a listagem
    inteira (listar_datasets ou o resource catalogo://datasets) e escolher pelo assunto e pelo
    grao. Use buscar_dataset quando o catalogo for grande demais para ler de uma vez, ou para
    localizar datasets por um termo especifico (sigla, nome de coluna, entidade).

    Parametros:
        tema: A pergunta do usuario ou o assunto. A pergunta inteira ranqueia melhor que
              palavras-chave soltas; nao resuma.
        orgao: Opcional - filtra pelo orgao do dataset. Neste corpus o unico valor e 'ons'.

    Retorno:
        Candidatos com RELEVANCIA em faixa (alta/media/fraca) e os termos que casaram. Quando o
        melhor candidato e fraco, o cabecalho diz "Nenhum dataset relevante": chame
        listar_datasets. Confirme sempre com descrever_dataset antes de gerar SQL.
    """
    _ensure_catalog()
    return _append_temporal_footer(await _buscar(tema, catalog=CATALOG, orgao=orgao))


@mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True, "destructiveHint": False, "openWorldHint": False})
@log_tool_call
async def listar_datasets(orgao: str | None = None) -> str:
    """Lista TODOS os datasets do catalogo, um por linha: nome, granularidade, perspectiva
    temporal (verificado, programado, previsto ou cadastral) e uma frase de uso.

    Comece por aqui: e a entrada padrao do fluxo. Leia a listagem, escolha pelo assunto E pelo
    grao da pergunta (horario, diario ou mensal; verificado ou previsto) e chame
    descrever_dataset com o nome exato antes de gerar SQL. Cerca de 4 mil tokens. O mesmo
    conteudo esta no resource `catalogo://datasets`, que dispensa esta chamada quando fixado
    no contexto.

    Parametros:
        orgao: Opcional - filtra pelo orgao do dataset. Neste corpus o unico valor e 'ons'.

    Fluxo recomendado: listar_datasets -> descrever_dataset -> gerar query -> executar_sql.
    """
    _ensure_catalog()
    return _append_temporal_footer(_listar(catalog=CATALOG, orgao=orgao))


@mcp.resource(
    "catalogo://datasets",
    name="catalogo_datasets",
    title="Catalogo de datasets",
    description="Todos os datasets, um por linha: nome, granularidade, perspectiva temporal e uso.",
    mime_type="text/markdown",
)
async def catalogo_datasets() -> str:
    _ensure_catalog()
    return _listar(catalog=CATALOG)


@mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True, "destructiveHint": False, "openWorldHint": False})
@log_tool_call
async def descrever_dataset(nome_dataset: str) -> str:
    """Retorna uma visao operacional formatada do contrato ODCS de um dataset do ONS.

    Parametros:
        nome_dataset: Nome EXATO do dataset, como aparece em listar_datasets ou buscar_dataset.

    Retorno:
        Visao formatada com schema, colunas (nomes e tipos), instrucoes de TRY_CAST,
        fewShotQueries de exemplo e portal URL do ONS (projecao dos campos operacionais
        do contrato, nao o YAML ODCS integral).

    SEMPRE leia o contrato antes de gerar SQL — use os nomes de colunas exatos.
    Cite o nome do dataset e o portal URL como fonte na resposta ao usuario."""
    _ensure_catalog()
    return _append_temporal_footer(await _descrever_dataset(nome_dataset, catalog=CATALOG))


@mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": False, "destructiveHint": False, "openWorldHint": True})
@log_tool_call
async def executar_sql(sql_query: str, limit: int = 100, offset: int = 0) -> str:
    """Executa SQL DuckDB read-only sobre os dados abertos do ONS (S3) com paginacao.

    Parametros:
        sql_query: Query SQL (SELECT ou WITH) baseada nas colunas reais do contrato.
                   Somente leitura — DML/DDL sao bloqueados.
        limit: Maximo de linhas retornadas (default: 100).
        offset: Deslocamento para paginacao (default: 0).

    Retorno:
        Linhas do resultado da query. Quando ha mais paginas disponiveis, o retorno
        indica que existem registros adicionais alem do limit.

    IMPORTANTE: Use descrever_dataset antes de gerar SQL para obter nomes de colunas
    exatos e tipos. Nunca chute nomes de colunas. Verifique a cobertura temporal no
    contrato antes de filtrar por datas."""
    from mcp_tiago_dados_abertos.motor.pruning import prune_user_sql
    from mcp_tiago_dados_abertos.motor.unit_advisor import aviso_unidade
    from mcp_tiago_dados_abertos.resposta.provenance import bloco_sources, datasets_do_sql

    _ensure_catalog()
    # Poda por ano do glob escrito pelo LLM (contract-driven): sem isto, uma query
    # manual varre 2000-2026 p/ filtrar 1 ano (~30x mais lento). Fallback seguro.
    sql_exec = prune_user_sql(sql_query, CATALOG)
    # Metrica de qualidade: share de consultas resolvidas pela camada GOVERNADA
    # (sql_pattern do corpus) vs ad-hoc. Usa o SQL ORIGINAL (a poda reescreve o glob
    # e descasaria o pattern). Nunca bloqueia.
    try:
        from mcp_tiago_dados_abertos.infra.telemetry import evento
        from mcp_tiago_dados_abertos.resposta.provenance import rota_do_sql

        rota, pat = rota_do_sql(sql_query, CATALOG)
        evento(evt="sql_route", rota=rota, pattern=pat)
    except Exception:
        pass
    resultado = await _executar_sql(sql_exec, limit, offset, catalog=CATALOG)
    # Proveniencia SSOT: datasets REAIS do SQL (URI de read_parquet -> contrato).
    # SEM period aqui: em SQL manual um alias pode fabricar a coluna temporal
    # ('2099-01-01' AS din_instante) e o carimbo mentiria. Falha/indisponivel =>
    # sem bloco (nada foi lido).
    bloco = ""
    if not resultado.startswith(("[ERRO]", "[TIAGO Dados Abertos]")):
        bloco = bloco_sources(datasets_do_sql(sql_exec, CATALOG))
    # Advisory contract-driven: fator de conversao no SQL x interval_hours
    # declarado no contrato. Nunca bloqueia. (usa o SQL ORIGINAL do usuario)
    resultado += aviso_unidade(sql_query, CATALOG)
    return _append_temporal_footer(resultado) + bloco


# ── Health check ──────────────────────────────────────────────────────────────


@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> JSONResponse:
    from mcp_tiago_dados_abertos.execucao.db import DUCKDB_OK

    # Readiness real: so esta "healthy" quando pode de fato servir queries
    # (DuckDB inicializado E catalogo carregado). Durante o warmup em background,
    # ou se o DuckDB falhar, responde 503 — para orquestradores nao rotearem trafego.
    ready = bool(DUCKDB_OK and _catalog_loaded and len(CATALOG) > 0)
    return JSONResponse(
        {
            "status": "healthy" if ready else "unavailable",
            "server": "MCP TIAGO Dados Abertos",
            "version": VERSION,
            "duckdb": DUCKDB_OK,
            "catalog_loaded": _catalog_loaded,
            "total_contracts": len(CATALOG),
        },
        status_code=200 if ready else 503,
    )


# ── Catalogo dinamico ─────────────────────────────────────────────────────────


@mcp.custom_route("/datasets", methods=["GET"])
async def datasets(request: Request) -> JSONResponse:
    """Lista o catalogo real em runtime — fonte unica para docs dinamicas."""
    _ensure_catalog()
    items = []
    for meta in CATALOG.values():
        if meta.get("status") == "discontinued":
            continue
        sem = meta.get("semantics") or {}
        routing = sem.get("query_routing") or {}
        supports = [k.replace("supports_", "") for k, v in routing.items() if v is True and k.startswith("supports_")]
        items.append(
            {
                "name": meta["name"],
                "granularity": meta.get("granularity", ""),
                "coverage": [meta.get("period_start", ""), meta.get("period_end", "")],
                "metrics": [m.get("id") for m in (sem.get("metrics") or []) if isinstance(m, dict)],
                "supports": supports,
                "has_sql_patterns": bool(sem.get("sql_patterns")),
                "portal_url": meta.get("portal_url", ""),
            }
        )
    items.sort(key=lambda x: x["name"])
    return JSONResponse({"total": len(items), "datasets": items})


# ── Entry point ───────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> None:
    """Entrada do console script `mcp-tiago-dados-abertos` e de `python -m ...server`."""
    import argparse
    import os

    parser = argparse.ArgumentParser(prog="mcp-tiago-dados-abertos")
    parser.add_argument("--stdio", action="store_true", help="Run in stdio mode (for IDE integration)")
    args = parser.parse_args(argv)

    if args.stdio:
        _ensure_catalog()
        print(f"[MCP TIAGO Dados Abertos v{VERSION}] stdio mode | {len(CATALOG)} contratos", file=sys.stderr)
        mcp.run(transport="stdio")
    else:
        stateless = os.getenv("MCP_STATELESS", "true").lower() == "true"
        mode = "stateless" if stateless else "stateful"
        print(f"\n[MCP TIAGO Dados Abertos v{VERSION}] Operador Nacional do Sistema Eletrico", file=sys.stderr)
        print(f"[TIAGO Dados Abertos] Endpoint: http://localhost:{PORT}/mcp ({mode})", file=sys.stderr)
        mcp.run(
            transport="streamable-http",
            host=HOST,
            port=PORT,
            stateless_http=stateless,
            json_response=True,
        )


if __name__ == "__main__":
    main()
