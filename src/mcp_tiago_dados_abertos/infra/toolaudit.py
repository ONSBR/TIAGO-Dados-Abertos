# -*- coding: utf-8 -*-
"""Auditoria de tool calls para INSIGHTS de evolucao (nao para vigilancia).

Loga por chamada: nome da tool + argumentos (tema/SQL/pergunta) + result_meta
(tamanho, ok, latencia). NAO loga o resultado completo, nem o thread_id cru, nem
qualquer segredo. PII e REDIGIDA no proprio app ANTES de escrever: o valor cru nunca
entra no log, o que e mais forte que qualquer mascara aplicada na leitura.

Sai como 1 linha JSON PURA (handler proprio, propagate=False) no stderr. Chave
discriminante: evt="tool_call". Quem opera decide retencao e mascara adicional no
coletor de logs.
"""

import functools
import inspect
import json
import logging
import os
import re
import sys
import time

from mcp_tiago_dados_abertos.infra.reqctx import current_correlation_id

# Flag para logar output das tools (para debug/benchmark)
LOG_TOOL_OUTPUT = os.getenv("LOG_TOOL_OUTPUT", "").lower() in ("1", "true", "yes")
MAX_OUTPUT_CHARS = 2000  # Trunca output no log

# Logger dedicado com handler proprio -> JSON PURO em stderr (sem prefixo do basicConfig).
logger = logging.getLogger("mcp_tiago_dados_abertos.toolaudit")
if not logger.handlers:
    _h = logging.StreamHandler(sys.stderr)
    _h.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(_h)
logger.propagate = False
logger.setLevel(logging.INFO)

# ── Redacao de PII no app (padroes claros e de baixo falso-positivo) ─────────
# Ordem importa: tokens estruturados (JWT/Bearer/AWS) antes; CNPJ antes de CPF.
_PATTERNS = [
    (re.compile(r"eyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}"), "[JWT]"),
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{8,}=*"), "Bearer [TOKEN]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[AWS_KEY]"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[EMAIL]"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "[IP]"),
    (re.compile(r"\b\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}\b"), "[CNPJ]"),
    (re.compile(r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b"), "[CPF]"),
    (re.compile(r"\b0800[\s-]?\d{3}[\s-]?\d{4}\b"), "[TEL]"),
    (re.compile(r"\b(?:\+?55\s?)?(?:\(?\d{2}\)?\s?)?9?\d{4}-?\d{4}\b"), "[TEL]"),
]

MAX_VALUE_CHARS = 1000  # trunca args gigantes (limita o custo adversarial dos regexes)
DROP_ARGS = frozenset({"ctx", "context", "kwargs", "args"})

# Marcadores de erro GRACIOSO (tools retornam erro como string) -> ok=false
_ERROR_MARKERS = (
    "[erro]",
    "[rate limit]",
    "[limite",
    "nao autorizado",
    "nao encontrado",
    "request blocked",
)


def _redact(value):
    """Redige PII e trunca. Nao-strings simples passam como tipo; resto vira str truncada."""
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if not isinstance(value, str):
        value = str(value)
    s = value[:MAX_VALUE_CHARS]
    for pat, rep in _PATTERNS:
        s = pat.sub(rep, s)
    if len(value) > MAX_VALUE_CHARS:
        s += "...[TRUNC]"
    return s


def _clean_args(named):
    return {k: _redact(v) for k, v in named.items() if k not in DROP_ARGS}


def _result_ok(result, had_exception):
    """ok funcional: sem excecao E sem marcador de erro gracioso no inicio do resultado."""
    if had_exception:
        return False
    if isinstance(result, str):
        head = result[:200].lower()
        if any(m in head for m in _ERROR_MARKERS):
            return False
    return True


def log_tool_call(func):
    """Decorator para tools async: audita nome+args+result_meta com PII redigida.

    Aplicar ENTRE @mcp.tool(...) e a def. A assinatura e preservada para o FastMCP.
    A auditoria NUNCA quebra a tool (tudo protegido) e propaga cancelamento/erros.
    """
    sig = inspect.signature(func)

    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        start = time.monotonic()
        cpu0 = time.process_time()  # CPU do processo: separa fome-de-CPU de espera
        try:
            bound = sig.bind(*args, **kwargs)
            bound.apply_defaults()
            named = dict(bound.arguments)
        except Exception:
            named = dict(kwargs)

        result = None
        had_exc = False
        chars = -1
        try:
            result = await func(*args, **kwargs)
            if isinstance(result, str):
                chars = len(result)
            return result
        except BaseException:  # inclui CancelledError/timeout -> registra ok=false e propaga
            had_exc = True
            raise
        finally:
            try:
                # Correlation ID (CORR_HEADER; x-request-id por padrao) (protected - never breaks audit)
                try:
                    corr = current_correlation_id()
                except Exception:
                    corr = None

                payload = {
                    "evt": "tool_call",
                    "tool": func.__name__,
                    "args": _clean_args(named),
                    "ok": _result_ok(result, had_exc),
                    "chars": chars,
                    "latency_ms": round((time.monotonic() - start) * 1000, 1),
                    "cpu_ms": round((time.process_time() - cpu0) * 1000, 1),
                }
                # Add output if LOG_TOOL_OUTPUT is enabled (for debug/benchmark)
                if LOG_TOOL_OUTPUT and isinstance(result, str):
                    output_truncated = result[:MAX_OUTPUT_CHARS]
                    if len(result) > MAX_OUTPUT_CHARS:
                        output_truncated += "...[TRUNC]"
                    payload["output"] = output_truncated
                # Add corr only if truthy (don't pollute with null)
                if corr:
                    payload["corr"] = corr
                logger.info(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
            except Exception:
                pass  # auditoria NUNCA pode quebrar a tool

    wrapper.__signature__ = sig
    return wrapper
