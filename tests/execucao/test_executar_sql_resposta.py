# -*- coding: utf-8 -*-
"""A resposta de executar_sql: tabela + result-schema; sem bloco de fatos calculados.

A aditividade de cada coluna vem do contrato (resposta/additivity.py) e chega ao modelo pelo
result-schema; a tabela ja traz os valores. O caminho de erro devolve so a mensagem com dica.
"""

import asyncio


def _run(monkeypatch, cols, rows, md, sql, cat):
    from mcp_tiago_dados_abertos.catalogo import contracts as catmod
    from mcp_tiago_dados_abertos.execucao import db
    from mcp_tiago_dados_abertos.tools import executar_sql as mod

    async def fake_raw(s, limit=100, offset=0, thread_id=None):
        return True, md, cols, rows, False

    monkeypatch.setattr(db, "executar_sql_raw", fake_raw)
    monkeypatch.setattr(catmod, "load_all_contracts", lambda: cat)
    return asyncio.run(mod.executar_sql(sql))


def test_resposta_tem_tabela_e_result_schema_sem_computed_facts(monkeypatch):
    sql = "SELECT fonte, SUM(val) AS gwh FROM read_parquet('s3://b/x/*.parquet') GROUP BY fonte"
    md = "| fonte | gwh |\n|---|---|\n| h | 70.0 |\n| e | 30.0 |"
    cat = {"x": {"name": "x", "portal_url": "u", "parquet_source": "read_parquet('s3://b/x/*.parquet')",
                 "semantics": {"metrics": [{"id": "g", "columns": ["val"], "quantity_kind": "energy"}], "row_grain": {}}}}
    out = _run(monkeypatch, ["fonte", "gwh"], [("h", 70.0), ("e", 30.0)], md, sql, cat)
    assert out.startswith(md)
    assert "```result-schema" in out
    assert "computed-facts" not in out and '"facts"' not in out


def test_aditividade_no_result_schema_vem_do_contrato(monkeypatch):
    """SUM sobre metrica de fluxo (energy) e aditiva; AVG sobre preco nao e."""
    cat = {"x": {"name": "x", "portal_url": "u", "parquet_source": "read_parquet('s3://b/x/*.parquet')",
                 "semantics": {"metrics": [{"id": "g", "columns": ["val"], "quantity_kind": "energy"},
                                           {"id": "p", "columns": ["pld"], "quantity_kind": "price"}], "row_grain": {}}}}
    sql = "SELECT sub, SUM(val) AS gwh, AVG(pld) AS pld_medio FROM read_parquet('s3://b/x/*.parquet') GROUP BY sub"
    out = _run(monkeypatch, ["sub", "gwh", "pld_medio"], [("SE", 70.0, 300.0), ("S", 30.0, 280.0)],
               "| sub | gwh | pld_medio |\n|---|---|---|", sql, cat)
    schema = out.split("```result-schema")[1]
    gwh = schema[schema.index('"name": "gwh"'):]
    pld = schema[schema.index('"name": "pld_medio"'):]
    assert '"additive": true' in gwh[: gwh.index("}")]
    assert '"additive": false' in pld[: pld.index("}")]


def test_error_path_untouched(monkeypatch):
    from mcp_tiago_dados_abertos.execucao import db
    from mcp_tiago_dados_abertos.tools import executar_sql as mod

    async def fake_raw(sql, limit=100, offset=0, thread_id=None):
        return False, "Binder Error: x", [], [], False

    monkeypatch.setattr(db, "executar_sql_raw", fake_raw)
    out = asyncio.run(mod.executar_sql("SELECT 1"))
    assert out.startswith("[ERRO]") and "result-schema" not in out
