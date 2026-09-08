# -*- coding: utf-8 -*-
"""Controles de abuso/DoS para exposicao publica.

Testa:
- Rate-limit GLOBAL (fecha o bypass por rotacao de chave)
- Thread-safety do RateLimiter (lock) — roda sob asyncio.to_thread
- Teto de chaves + truncagem de chave gigante (anti-OOM) no limiter
- Emissao da linha JSON `evt=sql` (metrica para o coletor de logs)

NOTA: O circuit-breaker cross-call por thread_id foi REMOVIDO. O anti-abuso real
no processo. O retry interno do pipeline analisar usa contador LOCAL.
"""

import asyncio
import io
import json
import logging
import threading

import pytest

from mcp_tiago_dados_abertos.execucao.rate_limiter import RateLimiter


# ── Teto de chaves + chave gigante (RateLimiter) ────────────────────
def test_rate_limiter_caps_bucket_keys():
    rl = RateLimiter(max_requests=100, window_seconds=60, burst=100, max_keys=10)
    for i in range(200):
        rl.allow(f"key-{i}")
    assert rl.active_keys <= 10


def test_rate_limiter_truncates_giant_key():
    from mcp_tiago_dados_abertos.execucao.rate_limiter import _MAX_KEY_LEN

    rl = RateLimiter(max_requests=100, window_seconds=60, burst=100)
    rl.allow("x" * 1_000_000)
    assert all(len(k) <= _MAX_KEY_LEN for k in rl._buckets)


# ── Thread-safety (serialização via seam do time.monotonic) ─
def test_rate_limiter_serializes_concurrent_allow(monkeypatch):
    import time

    import mcp_tiago_dados_abertos.execucao.rate_limiter as rlmod

    rl = RateLimiter(max_requests=10_000, window_seconds=300, burst=10_000_000)
    real = time.monotonic
    state = {"inside": 0, "overlap": False}
    probe_lock = threading.Lock()

    def probe():
        # allow() chama time.monotonic() DENTRO da secao critica. time.sleep libera o
        # GIL: sem lock no allow(), outra thread entra e 'inside' passa de 1 -> overlap.
        with probe_lock:
            state["inside"] += 1
            if state["inside"] > 1:
                state["overlap"] = True
        time.sleep(0.002)
        with probe_lock:
            state["inside"] -= 1
        return real()

    monkeypatch.setattr(rlmod.time, "monotonic", probe)
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        rl.allow("k")

    ts = [threading.Thread(target=worker) for _ in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert not state["overlap"], "allow() rodou concorrente (sem lock)"


# ── Rate-limit GLOBAL fecha rotação de chave ────────────────────────────
def test_global_limit_blocks_key_rotation():
    rl = RateLimiter(max_requests=1000, window_seconds=300, burst=1_000_000, global_max=5)
    results = [rl.allow(f"rotated-{i}")[0] for i in range(20)]
    assert sum(results) <= 5  # trocar a chave a cada request nao passa do teto global


# ── Métrica evt=sql ─────────────────────────────────────────────────────────
def _attach_metric_capture():
    from mcp_tiago_dados_abertos.execucao import db

    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    h.setFormatter(logging.Formatter("%(message)s"))
    db.metrics_logger.addHandler(h)
    return db, buf, h


def test_emit_sql_metric_emits_pure_json_line():
    db, buf, h = _attach_metric_capture()
    try:
        db._emit_sql_metric(ok=True, latency_ms=12.3)
    finally:
        db.metrics_logger.removeHandler(h)
    line = buf.getvalue().strip().splitlines()[-1]
    payload = json.loads(line)  # tem de ser JSON PURO (sem prefixo de log) p/ o metric filter
    assert payload["evt"] == "sql"
    assert payload["ok"] is True
    assert "latency_ms" in payload


@pytest.mark.skipif(
    not __import__("mcp_tiago_dados_abertos.execucao.db", fromlist=["DUCKDB_OK"]).DUCKDB_OK,
    reason="sem duckdb",
)
def test_executar_sql_emits_metric():
    db, buf, h = _attach_metric_capture()
    try:
        ok, out = asyncio.run(db.executar_sql("SELECT 1 AS x", limit=5))
    finally:
        db.metrics_logger.removeHandler(h)
    assert ok, out
    metrics = [json.loads(li) for li in buf.getvalue().strip().splitlines() if li.strip().startswith("{")]
    assert any(m.get("evt") == "sql" and m.get("ok") is True for m in metrics)


# ── Retry interno do analisar (contador local) ────────────────────────────
