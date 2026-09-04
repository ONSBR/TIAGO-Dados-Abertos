# -*- coding: utf-8 -*-
"""Telemetria mínima do MCP: 1 evento JSON por tool-call + contadores em memória.

Regras que protegem o desempenho e o protocolo:
- stdout é o canal JSON-RPC no modo stdio: telemetria escreve APENAS no stderr,
  em logger próprio com propagate=False e linha JSON PURA (sem prefixo de
  logging) — assim um coletor de logs parseia direto, sem regex.
- NUNCA loga payload de resposta (só tamanhos/contagens) nem SQL completo, que
  pode carregar URL tokenizada; params de pattern sim (não carregam credencial).
- Só stdlib já carregada (json/logging/time): zero regressão de cold start.
  Emissão custa ~µs vs tool-calls de segundos.
- Telemetria NUNCA derruba a chamada: qualquer exceção aqui é engolida.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from collections import Counter


class _StderrDinamico(logging.Handler):
    """Resolve sys.stderr NA EMISSÃO (não no import): sobrevive ao rewrap UTF-8
    do entry point no win32 e ao capture do pytest."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            print(record.getMessage(), file=sys.stderr)
        except Exception:  # noqa: BLE001
            pass


_LOG = logging.getLogger("mcp_tiago_dados_abertos.telemetry")
_LOG.propagate = False  # sem prefixo do basicConfig: linha = JSON puro
if not _LOG.handlers:
    _LOG.addHandler(_StderrDinamico())
_LOG.setLevel(logging.INFO)

_START = time.time()
CONTADORES: Counter = Counter()

# Taxonomia enxuta (porta de generation/validation_error_taxonomy do flow):
# 'estrutural' = fato sobre template/schema; o resto é transiente/valor.
_ESTRUTURAL = ("binder error", "parser error", "catalog error", "conversion error")
_REDE = ("http", "connection", "getaddrinfo", "timed out", "ssl", "certificate",
         "502", "503", "404", "403")


def classe_erro(texto: str | None) -> str | None:
    """Classifica a mensagem de erro numa classe estável p/ métrica."""
    if not texto:
        return None
    t = str(texto).lower()
    if "bloqueada" in t and "validador" in t or "nao permitida" in t:
        return "validador"
    if "timeout" in t or "tempo limite" in t:
        return "timeout"
    if any(k in t for k in _ESTRUTURAL):
        return "estrutural"
    if any(k in t for k in _REDE):
        return "rede"
    if "0 linhas" in t or "retornou 0" in t:
        return "vazio"
    return "outro"


def evento(**campos) -> None:
    """Emite 1 linha JSON no stderr e atualiza contadores. Nunca lança."""
    try:
        campos.setdefault("ts", round(time.time(), 3))
        campos = {k: v for k, v in campos.items() if v is not None}
        CONTADORES[f"evt:{campos.get('evt', '?')}"] += 1
        if campos.get("tool"):
            CONTADORES[f"tool:{campos['tool']}"] += 1
        if campos.get("ok") is False:
            CONTADORES["erros"] += 1
        if campos.get("classe_erro"):
            CONTADORES[f"erro:{campos['classe_erro']}"] += 1
        for flag in ("fallback", "clarify", "redirect", "cache"):
            if campos.get(flag):
                CONTADORES[flag] += 1
        _LOG.info(json.dumps(campos, ensure_ascii=False, default=str))
    except Exception:  # noqa: BLE001 — telemetria nunca derruba a chamada
        pass


def snapshot() -> dict:
    """Estado p/ a rota /health."""
    return {"uptime_s": round(time.time() - _START, 1), "contadores": dict(CONTADORES)}
