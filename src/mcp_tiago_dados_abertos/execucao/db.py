# -*- coding: utf-8 -*-
"""Conexao DuckDB e executor SQL.

Modernizacoes:
- Logging em stderr (nunca stdout — preserva o canal JSON-RPC do stdio).
- Execucao em thread (asyncio.to_thread) — nao bloqueia o event loop do servidor.
- Cursor por query — evita corromper a conexao compartilhada sob concorrencia.
- Formatacao de resultado sem pandas (DuckDB nativo + tabulate) — imagem leve.
"""

import asyncio
import functools
import heapq
import json
import logging
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from mcp_tiago_dados_abertos.execucao.validator import SqlValidator
from mcp_tiago_dados_abertos.infra.config import (
    DUCKDB_MEMORY_LIMIT,
    MAX_PAGE_LIMIT,
    MCP_HTTP_UA,
    QUERY_TIMEOUT_S,
)
from mcp_tiago_dados_abertos.infra.reqctx import current_correlation_id

logger = logging.getLogger("mcp_tiago_dados_abertos.db")

# Metrica de execucao SQL: 1 linha JSON PURA (sem prefixo de log) por query, em stderr.
# JSON puro para um coletor de logs filtrar por `{ $.evt = "sql" }` sem precisar de
# regex: por isso um handler proprio com formato "%(message)s" e propagate=False.
metrics_logger = logging.getLogger("mcp_tiago_dados_abertos.metrics")
if not metrics_logger.handlers:
    _mh = logging.StreamHandler(sys.stderr)
    _mh.setFormatter(logging.Formatter("%(message)s"))
    metrics_logger.addHandler(_mh)
metrics_logger.propagate = False
metrics_logger.setLevel(logging.INFO)


def _emit_sql_metric(ok: bool, latency_ms: float, rows: int = -1) -> None:
    """Emite a metrica de execucao SQL (evt=sql) para o coletor de logs."""
    payload = {
        "evt": "sql",
        "ok": bool(ok),
        "latency_ms": round(float(latency_ms), 1),
    }
    if rows >= 0:
        payload["rows"] = rows
    # Correlation ID (CORR_HEADER; x-request-id por padrao) (protected - never breaks metric emission)
    try:
        corr = current_correlation_id()
        if corr:
            payload["corr"] = corr
    except Exception:
        pass
    metrics_logger.info(json.dumps(payload, separators=(",", ":")))


# .env e carregado no config.py (antes dos os.getenv) — nao recarregar aqui.

# ── Threads pre-criadas no BOOT ──────────────────────────────────────────────
# Em ambiente de arranque a frio (serverless, container recem-subido), criar uma
# thread de SO chegou a custar ~20s por chamada (medido via dump de stack:
# threading.Thread.start() preso em _started.wait()). Antes, CADA query
# pagava isso 1-2x: threading.Timer novo (watchdog) + worker novo do executor
# default do asyncio.to_thread. Solucao: executor DEDICADO pre-aquecido no boot
# + UM watchdog singleton com heap de deadlines — zero criacao de thread por
# request.
_SQL_EXEC = ThreadPoolExecutor(max_workers=8, thread_name_prefix="tiago-dados-abertos-sql")
_warm = threading.Event()
for _ in range(8):
    _SQL_EXEC.submit(_warm.set)  # forca a criacao das 8 threads agora (boot)
_warm.wait(timeout=300)


class _Watchdog:
    """Singleton: interrompe cursores DuckDB que estouram o deadline.

    Substitui o threading.Timer por-query (que criava thread nova a cada SQL).
    Uma unica thread daemon percorre um heap de (deadline, cursor, state).
    """

    def __init__(self) -> None:
        self._cv = threading.Condition()
        self._heap: list = []
        self._seq = 0
        threading.Thread(target=self._loop, daemon=True, name="tiago-dados-abertos-watchdog").start()

    def arm(self, timeout_s: float, cur, state: dict) -> None:
        with self._cv:
            self._seq += 1
            heapq.heappush(self._heap, (time.monotonic() + timeout_s, self._seq, cur, state))
            self._cv.notify()

    def _loop(self) -> None:
        while True:
            with self._cv:
                while not self._heap:
                    self._cv.wait()
                deadline, _, cur, state = self._heap[0]
                agora = time.monotonic()
                if deadline > agora:
                    self._cv.wait(deadline - agora)
                    continue
                heapq.heappop(self._heap)
            if not state.get("done"):
                state["timed_out"] = True
                try:
                    cur.interrupt()  # mata a query em andamento neste cursor
                except Exception:
                    pass


_watchdog = _Watchdog()

async def _in_exec(fn, /, *args, **kwargs):
    """asyncio.to_thread no executor PRE-AQUECIDO (thread nova pode custar ~20s a frio)."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_SQL_EXEC, functools.partial(fn, *args, **kwargs))


# Inicializa DuckDB (conexao base — usada apenas para criar cursores)
try:
    import duckdb

    con = duckdb.connect(":memory:")
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute("SET memory_limit=?;", [DUCKDB_MEMORY_LIMIT])  # teto de memoria (anti-OOM)
    con.execute("SET s3_region='us-west-2';")  # ONS parquet (bucket publico)
    con.execute("SET s3_endpoint='s3.amazonaws.com';")
    con.execute("SET s3_url_style='path';")
    con.execute("SET http_timeout=120000;")
    con.execute("SET enable_http_metadata_cache=true;")
    # ONS e bucket PUBLICO (open data) — leitura ANONIMA. NAO injetamos credencial AWS no
    # DuckDB de proposito: impede que um SQL injetado leia buckets PRIVADOS da org usando a
    # chave estatica (ex.: read_parquet('s3://bucket-privado/...')). A listagem de
    # particoes via boto3 (motor/pruning.py) tambem e anonima. Validado ao vivo: leitura
    # anonima do ons-aws-prod-opendata funciona sem chave.
    # Secret HTTP default: User-Agent das leituras https (read_csv/read_json). Nao se
    # aplica a s3://, que e a origem de todo o corpus ONS.
    try:
        # Valor vem do operador (env), mas uma aspa simples derrubaria a conexao inteira:
        # escapa como literal SQL.
        _ua = MCP_HTTP_UA.replace("'", "''")
        con.execute(f"CREATE SECRET http_ua (TYPE HTTP, EXTRA_HTTP_HEADERS MAP{{'User-Agent': '{_ua}'}})")
    except Exception as _e:
        logger.warning("secret http_ua nao criado: %s", _e)
    validator = SqlValidator(con)
    DUCKDB_OK = True
    logger.info("DuckDB conectado (multi-fonte: S3 ONS + HTTP CSV/JSON).")
except Exception as e:  # pragma: no cover - ambiente sem duckdb
    con = None
    validator = None
    DUCKDB_OK = False
    logger.warning("DuckDB indisponivel: %s", e)


# Erro TRANSIENTE de rede na fonte (leitor HTTP/S3 do DuckDB): SSL, conexao, reset,
# 5xx do servidor, resolucao de nome. NAO inclui 401/403/404 (permissao/ausencia) nem
# timeout do watchdog (custo, nao rede).
_TRANSIENTE_RE = re.compile(
    r"SSL connect error|SSL_ERROR|Connection (?:error|reset|refused|timed out)|Could not establish connection"
    r"|HTTP (?:GET|HEAD) error.*?\b50[234]\b|\b50[234]\b.*?HTTP|Temporary failure in name resolution|EOF occurred",
    re.IGNORECASE,
)
_TRANSIENTE_ESPERA_S = 1.5


def _e_transiente(msg: str) -> bool:
    return bool(msg) and bool(_TRANSIENTE_RE.search(str(msg)))


# ── Formatacao sem pandas ─────────────────────────────────────────────────────


def _to_markdown(columns: list[str], rows: list[tuple]) -> str:
    """Formata um resultado em tabela markdown usando tabulate."""
    from tabulate import tabulate

    return tabulate(rows, headers=columns, tablefmt="github", disable_numparse=True)


# ── Participacao % deterministica ─────────────────────────────────────────────
_TEMPORAL_COL_RE = re.compile(
    r"(?:^|_)(din|dat|data|date|periodo|mes|ano|dia|instante|competencia|hora|hor|time|timestamp|semana|week)(?:_|$)",
    re.I,
)
_ID_COL_RE = re.compile(r"(?:^|_)(cod|codigo|id|num|ano|year|ceg|ranking)(?:_|$)", re.I)
_RELATIVA_COL_RE = re.compile(r"(pct|percent|taxa|indice|fator|ratio|propor)", re.I)


def _participacao_footer(columns: list[str], rows: list[tuple]) -> str:
    """Participacao % deterministica para resultado com cara de BREAKDOWN
    (exatamente 1 coluna de MEDIDA positiva + dimensao nao-temporal, 2-12 linhas
    COMPLETAS). Aritmetica derivada ("X% do total") e do SERVIDOR: LLMs clientes
    comprovadamente inventam percentuais ao deriva-los de cabeca da tabela.
    Gates por CLASSE de nome (não por dataset): colunas de codigo/ano/percentual
    nao sao medida; colunas temporais nao sao dimensao de share."""
    if not (2 <= len(rows) <= 12) or len(columns) < 2:
        return ""
    if any("participacao" in c.lower() for c in columns):
        return ""  # o motor ja calculou a coluna; nao duplicar

    def _num(v):
        if v is None or isinstance(v, bool):
            return None
        try:
            return float(str(v).replace(",", "."))
        except (TypeError, ValueError):
            return None

    num_cols = []
    for i in range(len(columns)):
        if _ID_COL_RE.search(columns[i]) or _RELATIVA_COL_RE.search(columns[i]) or _TEMPORAL_COL_RE.search(columns[i]):
            continue  # codigo/ano/relativa/temporal nunca e a medida do share
        vals = [_num(r[i]) for r in rows]
        if all(v is not None for v in vals):
            num_cols.append((i, vals))
    if len(num_cols) != 1:
        return ""  # ambiguo (0 ou 2+ colunas de medida): nao anota
    i, vals = num_cols[0]
    if any(v <= 0 for v in vals):
        return ""  # participacao so faz sentido com valores positivos
    if max(vals) <= 100.0:
        return ""  # faixa 0-100 tem cara de percentual/razao com nome opaco
    dim = next((j for j in range(len(columns)) if j != i and not _TEMPORAL_COL_RE.search(columns[j])), None)
    if dim is None:
        return ""  # serie temporal pura: share por periodo e ruido, nao insight
    total = sum(vals)
    partes = ", ".join(f"{str(rows[k][dim]).strip()}: {100 * v / total:.1f}%" for k, v in enumerate(vals))
    return f"\n\n*[participacao sobre o total das linhas: {partes}]*"


# ── Execucao ──────────────────────────────────────────────────────────────────


def _paginate(sql: str, limit: int, offset: int) -> tuple[str, bool]:
    """Empurra LIMIT(+1)/OFFSET para o SQL, envelopando SELECT/WITH numa subquery.

    Assim o DuckDB so produz a pagina pedida (+1 p/ detectar has_more) em vez de
    materializar o resultado inteiro em memoria (fetchall). Retorna (sql_executavel,
    envelopado). DESCRIBE e afins nao sao envelopados (resultado pequeno).
    """
    s = sql.strip().rstrip(";").strip()
    su = s.upper()
    if su.startswith("SELECT") or su.startswith("WITH"):
        return f"SELECT * FROM (\n{s}\n) AS _pg LIMIT {limit + 1} OFFSET {offset}", True
    return s, False


def _run_sync(sql: str, limit: int, offset: int) -> tuple[bool, str, list, list, bool]:
    """Valida, executa (cursor isolado + timeout via watchdog) e pagina. Roda em thread.

    Returns:
        (sucesso, resultado_md, columns, rows_crus, truncated)
        - truncated: True if result was paginated (has_more or offset>0)
        - Em caso de erro: (False, msg_erro, [], [], False)
        - Em caso de sucesso: (True, markdown, columns, rows, truncated)
    """
    ok, msg = validator.validate(sql)
    if not ok:
        return False, msg, [], [], False

    limit = max(1, min(int(limit), MAX_PAGE_LIMIT))  # clamp anti-DoS
    offset = max(0, int(offset))
    paged_sql, wrapped = _paginate(sql, limit, offset)

    cur = None
    state = {"timed_out": False, "done": False}
    try:
        cur = con.cursor()  # cursor isolado — seguro sob concorrencia
        # Watchdog singleton — NAO cria thread por query (thread nova custa ~20s
        # a frio; ver comentario do _Watchdog).
        _watchdog.arm(QUERY_TIMEOUT_S, cur, state)
        cur.execute(paged_sql)
        columns = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchall()
    except Exception as e:
        if state["timed_out"] or "INTERRUPT" in str(e).upper():
            return False, (
                f"Query excedeu o tempo limite ({QUERY_TIMEOUT_S:g}s) e foi interrompida. "
                "Refine os filtros (datas/agente/periodo) ou reduza o escopo."
            ), [], [], False
        return False, str(e)[:400], [], [], False
    finally:
        state["done"] = True
        if cur is not None:
            cur.close()

    if not rows:
        return True, "Query retornou 0 linhas.", [], [], False

    if wrapped:
        # LIMIT/OFFSET ja aplicados no engine; +1 linha detecta has_more
        has_more = len(rows) > limit
        page = rows[:limit]
        resultado = _to_markdown(columns, page)
        # Participacao % so quando o breakdown esta COMPLETO na pagina (sem more/offset)
        if not has_more and not offset:
            resultado += _participacao_footer(columns, page)
        if has_more or offset:
            next_offset = offset + limit if has_more else offset + len(page)
            resultado += (
                f"\n\n*[Paginacao: exibindo {offset + 1}-{offset + len(page)}"
                f" | has_more={has_more} | next_offset={next_offset}]*"
            )
        # v3.1: truncated = has_more OR offset>0 (result is a page, not full)
        truncated = has_more or offset > 0
        return True, resultado, columns, list(page), truncated

    # Nao-envelopado (DESCRIBE): resultado pequeno, slice em python
    total = len(rows)
    page = rows[offset : offset + limit]
    resultado = _to_markdown(columns, page)
    truncated = False
    if total > limit:
        resultado += (
            f"\n\n*[Paginacao: exibindo {offset + 1}-{min(offset + limit, total)} de {total} linhas"
            f" | has_more={offset + limit < total} | next_offset={offset + limit}]*"
        )
        truncated = offset + limit < total or offset > 0
    return True, resultado, columns, list(page), truncated


def _portal_do_sql(sql: str) -> str | None:
    """Rótulo do portal alvo p/ telemetria (host, nunca a URL completa/token)."""
    import re as _re

    if "s3://" in sql:
        return "ons-s3"
    m = _re.search(r"https?://([^/'\s]+)", sql)
    return m.group(1) if m else None


async def executar_sql(
    sql: str, limit: int = 100, offset: int = 0, thread_id: str | None = None
) -> tuple[bool, str]:
    """Valida, executa e pagina resultados. Retorna (sucesso, resultado).

    thread_id (rate-limit) e OPCIONAL: quando o motor semantico
    chama sem ele, resolve pelo correlation_id da request (reqctx) ou 'anon'. Assim
    a mesma assinatura serve o caminho de tool (sem thread_id) e chamadas diretas.

    Instrumentado: 1 evento JSON (evt=sql) por execução — esta é a camada que
    TODOS os caminhos atravessam (tools, analisar, evals), então a telemetria
    de execução cobre o sistema inteiro num ponto só."""
    from mcp_tiago_dados_abertos.infra.telemetry import classe_erro, evento

    thread_id = thread_id or current_correlation_id() or "anon"
    t0 = time.perf_counter()
    sucesso, resultado, _cols, _rows, _trunc = await _executar_sql_nucleo(sql, thread_id, limit, offset)
    latency_ms = round((time.perf_counter() - t0) * 1000)
    # telemetria (portal/thread_id) + metrica (evt=sql puro + correlation_id)
    evento(evt="sql", thread_id=thread_id, ok=sucesso,
           latency_ms=latency_ms,
           portal=_portal_do_sql(sql), resp_bytes=len(resultado),
           classe_erro=classe_erro(resultado) if not sucesso else None)
    _emit_sql_metric(sucesso, latency_ms)
    return sucesso, resultado


async def executar_sql_raw(
    sql: str, limit: int = 100, offset: int = 0, thread_id: str | None = None
) -> tuple[bool, str, list, list, bool]:
    """Valida, executa e pagina resultados. Retorna (sucesso, resultado, columns, rows, truncated).

    Versao estendida de executar_sql que inclui columns e rows crus (nao markdown).
    Usada pelo analisar para computed_facts.

    v3.1: Returns truncated flag (True if result is a page, not full result).
    """
    from mcp_tiago_dados_abertos.infra.telemetry import classe_erro, evento

    thread_id = thread_id or current_correlation_id() or "anon"
    t0 = time.perf_counter()
    sucesso, resultado, columns, rows, truncated = await _executar_sql_nucleo(sql, thread_id, limit, offset)
    latency_ms = round((time.perf_counter() - t0) * 1000)
    evento(evt="sql", thread_id=thread_id, ok=sucesso,
           latency_ms=latency_ms,
           portal=_portal_do_sql(sql), resp_bytes=len(resultado),
           classe_erro=classe_erro(resultado) if not sucesso else None)
    _emit_sql_metric(sucesso, latency_ms)
    return sucesso, resultado, columns, rows, truncated


async def _executar_sql_nucleo(
    sql: str, thread_id: str, limit: int = 100, offset: int = 0
) -> tuple[bool, str, list, list, bool]:
    """Núcleo de executar_sql (sem telemetria — o wrapper acima instrumenta).

    Returns: (sucesso, resultado_md, columns, rows_crus, truncated)
    """
    if not DUCKDB_OK:
        return False, "[TIAGO Dados Abertos] DuckDB indisponivel.", [], [], False

    # Rate limiter — protege o S3 contra abuso
    from mcp_tiago_dados_abertos.execucao.rate_limiter import limiter

    allowed, rate_msg = limiter.allow(thread_id)
    if not allowed:
        return False, rate_msg, [], [], False

    # Validacao ANTES de qualquer side effect de rede: SQL
    # invalido nao pode disparar resolucao CKAN. _run_sync revalida (barato).
    ok_v, msg_v = validator.validate(sql)
    if not ok_v:
        return False, msg_v, [], [], False

    # Execucao bloqueante delegada a thread — mantem o event loop livre
    sucesso, resultado, columns, rows, truncated = await _in_exec(_run_sync, sql, limit, offset)

    # Retry-on-transiente: erro de REDE na fonte (SSL/conexao/5xx do leitor HTTP)
    # ganha UMA nova tentativa apos espera curta; se persistir, a mensagem diz que e
    # temporario. Erro de SQL/binder nunca repete (nao e transiente).
    if not sucesso and _e_transiente(resultado):
        await asyncio.sleep(_TRANSIENTE_ESPERA_S)
        sucesso, resultado, columns, rows, truncated = await _in_exec(_run_sync, sql, limit, offset)
        if not sucesso and _e_transiente(resultado):
            resultado = (f"{resultado} [erro TEMPORARIO de rede na fonte apos 2 tentativas — "
                         "tente novamente em alguns segundos]")

    return sucesso, resultado, columns, rows, truncated
