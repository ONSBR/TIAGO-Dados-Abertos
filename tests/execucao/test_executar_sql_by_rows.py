# -*- coding: utf-8 -*-
"""Integracao no tool executar_sql: by_rows com aditividade PROVADA pelo contrato
(lineage do SQL) e rollup do contrato; rodape rotulado so com >=2 medidas com pct."""
import asyncio

CFURH_COLS = ["subsistema", "nom_subsistema", "compensacao_total_rs", "qtd_usinas"]
CFURH_ROWS = [("SE", "SUDESTE", 20.0, 103), ("S", "SUL", 8.0, 31),
              ("N", "NORTE", 6.0, 9), ("NE", "NORDESTE", 4.0, 8),
              ("nao mapeado no ONS", "-", 2.0, 54)]
MD = "| subsistema | nom_subsistema | compensacao_total_rs | qtd_usinas |\n|---|---|---|---|\n| SE | SUDESTE | 20.0 | 103 |"
SQL_CFURH = ("SELECT o.id_subsistema AS subsistema, o.nom_subsistema, "
             "SUM(TRY_CAST(REPLACE(REPLACE(cf.VlrTotal, '.', ''), ',', '.') AS DOUBLE)) AS compensacao_total_rs, "
             "COUNT(DISTINCT cf.CodCEG) AS qtd_usinas "
             "FROM read_csv('https://x/cfurh.csv', auto_detect=true) cf LEFT JOIN o ON 1=1 GROUP BY 1, 2")
CAT = {"cfurh": {
    "name": "cfurh", "portal_url": "u",
    "parquet_source": "read_csv('https://x/cfurh.csv', auto_detect=true)",
    "semantics": {"metrics": [{"id": "compensacao_total", "columns": ["VlrTotal"], "quantity_kind": "financial_flow"}],
                  "row_grain": {}},
}}


def _run(monkeypatch, cols, rows, md, sql, cat):
    from mcp_tiago_dados_abertos.catalogo import catalog as catmod
    from mcp_tiago_dados_abertos.execucao import db
    from mcp_tiago_dados_abertos.tools import executar_sql as mod

    async def fake_raw(s, limit=100, offset=0, thread_id=None):
        return True, md, cols, rows, False

    monkeypatch.setattr(db, "executar_sql_raw", fake_raw)
    monkeypatch.setattr(catmod, "load_all_contracts", lambda: cat)
    return asyncio.run(mod.executar_sql(sql))


def test_aditivas_pelo_contrato_block_e_rodape(monkeypatch):
    out = _run(monkeypatch, CFURH_COLS, CFURH_ROWS, MD, SQL_CFURH, CAT)
    assert "```computed-facts" in out and '"facts"' in out  # legado by_rows saiu do bloco
    # compensacao (SUM sobre metrica de fluxo) ganha share; qtd_usinas e COUNT(DISTINCT):
    # nao-aditivo entre grupos => sem share (fail-closed). Com 1 so medida com pct, o rodape
    # multi-medida nao e emitido (o footer de 1 medida vive no markdown do db).
    assert '"share:compensacao_total_rs:se_sudeste"' in out or '"pct": 50.0' in out
    assert "qtd_usinas:" not in out.split("```computed-facts")[0]


def test_preco_media_sem_pct_sem_rodape(monkeypatch):
    sql = "SELECT sub, AVG(pld) AS pld_medio FROM read_parquet('s3://b/x/*.parquet') GROUP BY sub"
    out = _run(monkeypatch, ["sub", "pld_medio"], [("SE", 300.0), ("S", 280.0)],
               "| sub | pld_medio |\n|---|---|\n| SE | 300.0 |", sql, {})
    assert "participacao sobre" not in out
    # rank sim; share de preco NAO existe como fato (so como abstencao com reason_code)
    assert '"id": "rank:pld_medio:se"' in out and '"id": "share:' not in out
    assert '"reason_code": "non_additive"' in out


def test_one_measure_no_extra_footer(monkeypatch):
    md1 = "| fonte | gwh |\n|---|---|\n| h | 70.0 |\n\n*[participacao sobre o total das linhas: h: 70.0%, e: 30.0%]*"
    sql = "SELECT fonte, SUM(val) AS gwh FROM read_parquet('s3://b/x/*.parquet') GROUP BY fonte"
    cat = {"x": {"name": "x", "portal_url": "u", "parquet_source": "read_parquet('s3://b/x/*.parquet')",
                 "semantics": {"metrics": [{"id": "g", "columns": ["val"], "quantity_kind": "energy"}], "row_grain": {}}}}
    out = _run(monkeypatch, ["fonte", "gwh"], [("h", 70.0), ("e", 30.0)], md1, sql, cat)
    assert out.count("participacao sobre o total das linhas") == 1  # so o do db
    assert '"id": "share:gwh:h"' in out and '"value": 70.0' in out


def test_error_path_untouched(monkeypatch):
    from mcp_tiago_dados_abertos.execucao import db
    from mcp_tiago_dados_abertos.tools import executar_sql as mod

    async def fake_raw(sql, limit=100, offset=0, thread_id=None):
        return False, "Binder Error: x", [], [], False

    monkeypatch.setattr(db, "executar_sql_raw", fake_raw)
    out = asyncio.run(mod.executar_sql("SELECT 1"))
    assert out.startswith("[ERRO]") and "computed-facts" not in out
