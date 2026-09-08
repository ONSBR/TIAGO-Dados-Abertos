# -*- coding: utf-8 -*-
"""Paginacao server-side + timeout (anti-DoS).

Empurra LIMIT(+1)/OFFSET para o SQL (sem materializar tudo em memoria) e interrompe
queries longas via watchdog. Requer DuckDB; pula se indisponivel.
"""

import threading

import pytest

from mcp_tiago_dados_abertos.execucao import db

pytestmark = pytest.mark.skipif(not db.DUCKDB_OK, reason="DuckDB indisponivel")


def test_paginate_wraps_select():
    sql, wrapped = db._paginate("SELECT x FROM t ORDER BY x", 10, 20)
    assert wrapped is True
    assert sql.upper().startswith("SELECT * FROM (")
    assert "LIMIT 11" in sql and "OFFSET 20" in sql


def test_paginate_skips_describe():
    sql, wrapped = db._paginate("DESCRIBE foo", 10, 0)
    assert wrapped is False
    assert sql == "DESCRIBE foo"


def test_run_sync_pushes_limit_no_full_count():
    ok, out, _cols, _rows, _trunc = db._run_sync("SELECT x FROM range(500) AS r(x) ORDER BY x", 10, 0)
    assert ok, out
    assert "has_more=True" in out
    assert "next_offset=10" in out
    assert "de 500 linhas" not in out, "ainda contava o total (full scan)"
    assert "499" not in out, "trouxe alem da 1a pagina"


def test_run_sync_offset_last_page():
    ok, out, _cols, _rows, _trunc = db._run_sync("SELECT x FROM range(500) AS r(x) ORDER BY x", 10, 490)
    assert ok, out
    assert "has_more=False" in out
    assert "499" in out


def test_run_sync_clamps_huge_limit():
    # limit gigante nao deve estourar; e clampado para MAX_PAGE_LIMIT
    ok, out, _cols, _rows, _trunc = db._run_sync("SELECT x FROM range(5) AS r(x) ORDER BY x", 10_000_000, 0)
    assert ok, out


# ── Timeout: o watchdog, o caminho de erro, e a integracao ────────────────────
# Estes tres substituem um teste unico que era uma CORRIDA: ele dava 1ms de
# deadline e contava com `SELECT count(*) FROM range(1e6) CROSS JOIN range(100)`
# ser lento. Medido: essa query leva 19ms — o DuckDB resolve o count por
# CARDINALIDADE, sem materializar o produto. Margem de 25x contra o escalonador
# do SO, entao sob carga a query terminava antes do watchdog acordar e o teste
# falhava por ter ido rapido DEMAIS. Num CI publico isso reprova PR de terceiros.


def test_watchdog_interrompe_o_cursor():
    """O watchdog em si, sem DuckDB e sem corrida: espera ate 5s por um evento de 1ms."""
    disparou = threading.Event()

    class _CursorFalso:
        def interrupt(self):
            disparou.set()

    state = {"timed_out": False, "done": False}
    db._watchdog.arm(0.001, _CursorFalso(), state)
    assert disparou.wait(timeout=5.0), "watchdog nao interrompeu o cursor apos o deadline"
    assert state["timed_out"] is True


def test_interrupcao_vira_mensagem_honesta(monkeypatch):
    """O caminho de erro, deterministico: INTERRUPT vira recado acionavel, nao stacktrace."""

    class _CursorQueInterrompe:
        description = None

        def execute(self, _sql):
            raise RuntimeError("INTERRUPT: query was interrupted")

        def fetchall(self):
            return []

        def close(self):
            pass

    class _ConexaoFalsa:
        # o atributo `cursor` do objeto do DuckDB e' somente-leitura; troca-se a
        # conexao inteira, que e' atributo de modulo.
        def cursor(self):
            return _CursorQueInterrompe()

    monkeypatch.setattr(db, "con", _ConexaoFalsa())
    ok, out, _cols, _rows, _trunc = db._run_sync("SELECT 1", 10, 0)
    assert not ok
    assert "tempo limite" in out.lower() and "interromp" in out.lower(), out
    assert "refine" in out.lower(), "a mensagem tem que dizer o que fazer"


def test_query_lenta_e_interrompida(monkeypatch):
    """Integracao de verdade: query que NAO da' para otimizar por cardinalidade.

    O WHERE obriga a avaliar cada par do produto — medido em ~1,1s contra os 19ms
    do count(*) puro. Com deadline de 1ms, a margem passa de 25x para ~1000x.
    """
    monkeypatch.setattr(db, "QUERY_TIMEOUT_S", 0.001)
    sql = (
        "SELECT count(*) FROM range(1000000) t1 CROSS JOIN range(100) t2 "
        "WHERE (t1.range * t2.range) % 7 = 3"
    )
    ok, out, _cols, _rows, _trunc = db._run_sync(sql, 10, 0)
    assert not ok
    assert "interromp" in out.lower() or "timeout" in out.lower() or "cancel" in out.lower(), out


def test_real_s3_read_not_broken_by_filesystem_config():
    # REGRESSAO: SET disabled_filesystems='LocalFileSystem' quebrava TODA leitura S3
    # (glob/spill de parquet usam o LocalFileSystem) -> o servidor nao lia dado algum.
    # A protecao LFI fica no VALIDADOR (allowlist), nao no engine.
    import asyncio

    src = "s3://ons-aws-prod-opendata/dataset/balanco_energia_subsistema_ho/*.parquet"
    sql = f"SELECT * FROM read_parquet('{src}', union_by_name=true) LIMIT 3"
    ok, out = asyncio.run(db.executar_sql(sql, limit=3))
    assert "LocalFileSystem has been disabled" not in out, f"REGRESSAO: config bloqueia S3: {out}"
    if not ok:
        import pytest as _pt

        _pt.skip(f"S3 indisponivel (rede?): {out[:80]}")
    assert "|" in out  # markdown com dados reais


