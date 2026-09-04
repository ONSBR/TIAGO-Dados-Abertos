"""descrever_dataset — leitura e formatacao de contrato ODCS + semantics."""

import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from mcp_tiago_dados_abertos.catalogo.catalog import find, format_fewshots, format_schema
from mcp_tiago_dados_abertos.resposta.provenance import bloco_sources

# -- Probe de ultimo dado (cobertura honesta) ----------------------------------

_PROBE_TTL_S = 3600.0  # cache TTL 1h
_PROBE_TETO_S = 8.0  # teto TOTAL do probe (fail-silent)
_probe_cache: dict[str, tuple[float, str | None]] = {}
_probe_executor: ThreadPoolExecutor | None = None


def _get_temporal_column_from_meta(meta: dict) -> str | None:
    """Obtem coluna temporal de meta (reutiliza logica de valida_patterns)."""
    semantics = meta.get("semantics") or {}
    row_grain = semantics.get("row_grain") or {}
    tc = row_grain.get("temporal_column")
    if tc:
        return tc
    for col in meta.get("columns") or []:
        name = col.get("name", "")
        if name.startswith("din_") or name.startswith("dat_"):
            return name
        if (col.get("type") or "").upper() in ("TIMESTAMP", "DATE"):
            return name
    return None


def _probe_max_duckdb(parquet_source: str, col: str) -> str | None:
    """Executa MAX(col) via DuckDB. Retorna YYYY-MM-DD ou None."""
    from mcp_tiago_dados_abertos.execucao.db import con as db_con

    if db_con is None:
        return None
    cur = db_con.cursor()
    state = {"timed_out": False}
    timer = None
    try:

        def _interrupt():
            state["timed_out"] = True
            cur.interrupt()

        timer = threading.Timer(_PROBE_TETO_S, _interrupt)
        timer.daemon = True
        timer.start()
        sql = f"SELECT CAST(MAX(TRY_CAST({col} AS TIMESTAMP)) AS DATE) AS dmax FROM {parquet_source}"
        cur.execute(sql)
        rows = cur.fetchall()
        if rows and rows[0][0] is not None:
            return str(rows[0][0])[:10]
        return None
    except Exception:
        return None
    finally:
        if timer is not None:
            timer.cancel()
        cur.close()


def _ultimo_dado(meta: dict) -> str | None:
    """Data do ultimo dado MEDIDA agora (probe DuckDB), se COBERTURA_PROBE=1.

    Fail-silent com TETO TOTAL: timeout tambem vira None cacheado.
    O render NUNCA fica pendurado nem quebra por causa do probe.
    """
    if os.getenv("MCP_COBERTURA_PROBE") != "1":
        return None
    # So series temporais declaradas (com period_start e sem period_end = ativa)
    if not meta.get("period_start"):
        return None
    name = meta.get("name", "")
    agora = time.time()
    hit = _probe_cache.get(name)
    if hit and agora - hit[0] < _PROBE_TTL_S:
        return hit[1]
    valor: str | None = None
    try:
        col = _get_temporal_column_from_meta(meta)
        parquet_source = meta.get("parquet_source", "")
        if col and parquet_source:
            global _probe_executor
            if _probe_executor is None:
                _probe_executor = ThreadPoolExecutor(max_workers=1)
            fut = _probe_executor.submit(_probe_max_duckdb, parquet_source, col)
            valor = fut.result(timeout=_PROBE_TETO_S)
    except Exception:  # noqa: BLE001 - probe e melhoria, nunca bloqueia o render
        valor = None
    _probe_cache[name] = (agora, valor)
    return valor


def _fonte_legivel(source: str) -> str:
    """Fonte pronta para o agente COPIAR. Fonte de um-arquivo-por-periodo declara o
    placeholder no contrato (`{data_arquivo}` no diario, `{mes_arquivo}` no mensal); exibir o
    placeholder cru fazia o agente copiar a URL literal para o executar_sql.
    Aqui ele vira um exemplo CONCRETO — o ultimo publicado — com a regra dita em uma linha."""
    import datetime as _d

    src = str(source or "")
    hoje = _d.date.today()
    if "{data_arquivo}" in src:
        exemplo = (hoje - _d.timedelta(days=1)).strftime("%Y%m%d")
        nota = (
            "\n> Fonte com **um arquivo por dia** (AAAAMMDD no nome; o exemplo acima usa "
            f"{exemplo}, o ultimo publicado). Troque a data para consultar outro dia; "
            "para um PERIODO use o dataset mensal."
        )
        return src.replace("{data_arquivo}", exemplo) + nota
    if "{mes_arquivo}" in src:
        exemplo = hoje.strftime("%Y%m")
        nota = (
            "\n> Fonte com **um arquivo por mes** (AAAAMM no nome; o exemplo acima usa "
            f"{exemplo}, o mes corrente). Troque o mes para consultar outro; para varios "
            "meses passe a LISTA de arquivos ao `read_csv([...])` — um por mes da janela."
        )
        return src.replace("{mes_arquivo}", exemplo) + nota
    return src


def _format_cobertura(meta: dict) -> str:
    """Regra da cobertura honesta: serie ativa nao declara Fim — a
    ausencia significa 'ate o presente'; o probe (quando ligado) da o valor
    exato medido agora."""
    inicio = meta.get("period_start") or "?"
    period_end = meta.get("period_end") or ""
    freshness = meta.get("freshness") or ""

    # Sem inicio declarado (78 contratos): "? -> ?" nao diz nada ao agente. Diga o que o
    # dataset E — cadastro (granularity STATIC) nao tem serie temporal — ou que a cobertura
    # nao esta declarada, apontando COMO medir. Nunca inventar data.
    if inicio == "?":
        freq = f"; atualizacao {freshness}" if freshness else ""
        if str(meta.get("granularity") or "").upper() == "STATIC":
            return f"**Cobertura:** cadastro (snapshot da situacao atual, sem serie temporal{freq})"
        temporal = _get_temporal_column_from_meta(meta)
        como = f" — meca com SELECT MIN({temporal}), MAX({temporal})" if temporal else ""
        return f"**Cobertura:** nao declarada no contrato{freq}{como}"

    if not period_end:
        # Serie ATIVA
        freq_suffix = f"; atualizacao {freshness}" if freshness else ""
        base = f"**Cobertura:** {inicio} -> presente (serie ativa{freq_suffix})"
        medido = _ultimo_dado(meta)
        if medido:
            base += f" \u2014 ultimo dado medido agora: {medido}"
        return base
    # Serie encerrada ou cobertura completa declarada
    return f"**Cobertura:** {inicio} -> {period_end or '?'}"


def _format_semantics(meta: dict) -> str:
    """Resumo compacto do bloco semantics para orientar a geracao de SQL."""
    sem = meta.get("semantics") or {}
    if not sem:
        return ""
    lines = ["## Semantica (para gerar SQL correto)"]

    metrics = sem.get("metrics") or []
    if metrics:
        lines.append("**Metricas:**")
        for m in metrics:
            if not isinstance(m, dict):
                continue
            modes = ", ".join((m.get("aggregation_modes") or {}).keys())
            lines.append(
                f"- `{m.get('id')}` ({m.get('unit_native', '?')}, default: "
                f"{m.get('default_aggregation', '?')}) — modos: {modes}"
            )

    routing = sem.get("query_routing") or {}
    if routing:
        flags = [k.replace("supports_", "") for k, v in routing.items() if v is True and k.startswith("supports_")]
        if flags:
            lines.append(f"**Suporta:** {', '.join(flags)}")
        if routing.get("primary_use_case"):
            lines.append(f"**Uso principal:** {routing['primary_use_case']}")

    aliases = sem.get("value_aliases") or {}
    if aliases:
        lines.append(f"**Dimensoes filtraveis (com aliases):** {', '.join(aliases.keys())}")

    patterns = sem.get("sql_patterns") or {}
    if patterns:
        # Entregamos a SQL PROVADA, nao so' o nome: o cliente copia/adapta
        # esse andaime — que ja traz os casts (VARCHAR->DOUBLE), exclusoes (ex.:
        # 'SIN') e unidades corretos — em vez de reinventar a query e errar. Serve
        # de BASE para variantes (outro subsistema/periodo/recorte).
        lines.append(
            "**Padroes SQL prontos (copie/adapte a SQL provada abaixo - ela ja traz "
            "os casts, exclusoes e unidades corretos; nao reescreva do zero. Os "
            "placeholders {source}/{year}/{start}/{end}/{subsistema} sao resolvidos "
            "pelo executar_sql):**"
        )
        for nome, b in patterns.items():
            if not isinstance(b, dict):
                lines.append(f"- `{nome}`")
                continue
            unit = b.get("output_unit")
            lines.append(f"- `{nome}`" + (f" -- saida: {unit}" if unit else ""))
            tpl = b.get("template")
            if tpl:
                lines.append("  ```sql\n" + str(tpl) + "\n  ```")

    return "\n".join(lines)


def _is_value_column(col: dict) -> bool:
    """Detecta se uma coluna e de VALOR (metrica numerica).

    Criterio em 3 camadas: logicalType numerico do contrato, prefixo de
    nome, ou semanticType METRIC."""
    if (col.get("logical") or "").lower() in ("number", "integer"):
        return True
    name = col.get("name", "")
    if re.match(r"^(val_|qtd_)", name):
        return True
    if col.get("semantic_type") == "METRIC":
        return True
    return False


def _varchar_value_warnings(meta: dict) -> str:
    """Gera avisos para colunas de VALOR que sao VARCHAR no Parquet."""
    columns = meta.get("columns", [])
    warnings = []
    for col in columns:
        col_type = (col.get("type") or "").upper()
        if col_type in ("VARCHAR", "STRING") and _is_value_column(col):
            col_name = col.get("name", "")
            warnings.append(
                f"- `{col_name}`: [ATENCAO: valores como texto -- use TRY_CAST({col_name} AS DOUBLE) em agregacoes]"
            )
    return "\n".join(warnings)


async def descrever_dataset(nome_dataset: str, *, catalog: dict) -> str:
    """Retorna contrato ODCS completo de um dataset do ONS.

    Inclui schema, colunas, tipos, instrucoes de TRY_CAST, semantica
    (metricas/agregacoes/aliases), fewShotQueries e portal URL.

    Args:
        nome_dataset: Nome exato (use buscar_dataset para descobrir).
    """
    meta = find(catalog, nome_dataset)
    if not meta:
        return f"Dataset '{nome_dataset}' nao encontrado. Use buscar_dataset."

    schema = format_schema(meta)
    fqs = format_fewshots(meta)
    instr = meta.get("agent_instructions", "")
    semantics_block = _format_semantics(meta)
    varchar_warnings = _varchar_value_warnings(meta)

    parts = [
        f"# [ONS] {meta['name']}",
        f"**parquet_source:** `{_fonte_legivel(meta.get('parquet_source', 'N/A'))}`",
        f"**Granularidade:** {meta.get('granularity', '?')}",
        _format_cobertura(meta),
        "",
        schema,
    ]
    if varchar_warnings:
        parts += ["", "## Avisos de tipo (VARCHAR no Parquet)", varchar_warnings]
    if semantics_block:
        parts += ["", semantics_block]
    if instr:
        parts += ["", "## Instrucoes criticas", instr]
    if fqs:
        parts += ["", "## Exemplos SQL", fqs]
    if meta.get("portal_url"):
        parts += ["", f"*Fonte: {meta['portal_url']}*"]
    return "\n".join(parts) + bloco_sources([meta])
