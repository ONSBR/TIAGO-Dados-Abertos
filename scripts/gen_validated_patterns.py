# -*- coding: utf-8 -*-
"""Gerador-com-validacao de padroes para TODOS os datasets ONS S3 de energia.

Para cada coluna de potencia (unit MW/MWmed) gera 2 candidatos e VALIDA no DuckDB:
  energia_gwh    = SUM(val) * interval_hours / 1000
  potencia_mwmed = SUM(val) / COUNT(DISTINCT temporal)   (÷4-safe; exclui linha-total)
So promove o que a) executa, b) nao-NULL, c) passa o guard de plausibilidade fisica.
Emite inventario JSON: _inventario_padroes.json (dataset -> [{col, tipo, valor, sql_agg}]).

Uso: PYTHONPATH=src python scripts/gen_validated_patterns.py [--limit N]
"""
import argparse
import glob
import json
import sys

import duckdb
import yaml

sys.path.insert(0, "src")
from mcp_tiago_dados_abertos.resposta.result_validator import (  # noqa: E402
    _parse_markdown_table,
    _plausibilidade_fisica,
)

ANO = 2025


def _con():
    c = duckdb.connect(":memory:")
    c.execute("INSTALL httpfs; LOAD httpfs;")
    c.execute("SET s3_region='us-west-2'; SET s3_endpoint='s3.amazonaws.com';")
    c.execute("SET s3_url_style='path'; SET http_timeout=180000;")
    return c


def _cfg(d):
    loc = ""
    for s in (d.get("servers") or []):
        loc = s.get("location", "") or loc
    sem = {}
    for p in (d.get("customProperties") or []):
        if p.get("property") == "semantics":
            sem = p.get("value") or {}
    if not isinstance(sem, dict):
        sem = {}
    rg = sem.get("row_grain") or {}
    tcol = rg.get("temporal_column") or ""
    total = rg.get("has_total_row") or {}
    total_col, total_vals = total.get("column"), total.get("values") or []
    ivs = []
    for m in (sem.get("metrics") or []):
        if isinstance(m, dict) and m.get("interval_hours") is not None:
            try:
                ivs.append(float(m["interval_hours"]))
            except Exception:
                pass
    interval = max(set(ivs), key=ivs.count) if ivs else None
    pcols = []
    props = []
    for obj in (d.get("schema") or []):
        props += obj.get("properties", [])
    for c in props:
        cp = {p.get("property"): p.get("value") for p in (c.get("customProperties") or [])}
        if str(cp.get("unit", "")).lower() in ("mw", "mwmed") and (c.get("name") or "").lower().startswith("val"):
            pcols.append(c["name"])
    return loc, tcol, interval, sorted(set(pcols)), total_col, total_vals


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()
    con = _con()
    inv, resumo = {}, {"datasets": 0, "validados": 0, "implausivel": 0, "erro": 0, "vazio": 0}
    files = sorted(glob.glob("contracts/ons/*.odcs.yaml"))
    if args.limit:
        files = files[: args.limit]
    for f in files:
        try:
            d = yaml.safe_load(open(f, encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(d, dict):
            continue
        nome = d.get("name") or f
        loc, tcol, interval, pcols, tcol_total, tvals = _cfg(d)
        if not (loc and "s3://" in loc and tcol and interval and pcols):
            continue
        src = f"read_parquet('{loc}', union_by_name=true)"
        where = f"WHERE EXTRACT(YEAR FROM TRY_CAST({tcol} AS TIMESTAMP)) = {ANO}"
        if tcol_total and tvals:
            vv = ", ".join(f"'{v}'" for v in tvals)
            where += f" AND TRIM(CAST({tcol_total} AS VARCHAR)) NOT IN ({vv})"
        V = lambda c: f"TRY_CAST(TRY_CAST({c} AS VARCHAR) AS DOUBLE)"  # noqa: E731
        pats = []
        for col in pcols[:8]:
            e_sql = f"ROUND(SUM({V(col)}) * {interval} / 1000, 1)"
            p_sql = f"ROUND(SUM({V(col)}) / COUNT(DISTINCT TRY_CAST({tcol} AS TIMESTAMP)), 1)"
            for tipo, agg, unit in (("gwh", e_sql, "GWh"), ("mwmed", p_sql, "MWmed")):
                try:
                    (val,) = con.execute(f"SELECT {agg} v FROM {src} {where}").fetchone()
                except Exception:
                    resumo["erro"] += 1
                    continue
                if val is None:
                    resumo["vazio"] += 1
                    continue
                alias = f"{col}_{tipo}"
                flags = _plausibilidade_fisica(*_parse_markdown_table(f"| p | {alias} |\n|---|---|\n| x | {val} |"))
                if flags:
                    resumo["implausivel"] += 1
                    continue
                pats.append({"col": col, "tipo": tipo, "unit": unit, "valor": val, "agg_sql": agg})
                resumo["validados"] += 1
        if pats:
            inv[nome] = {"source": loc, "temporal": tcol, "interval_hours": interval,
                         "total_row": {"col": tcol_total, "vals": tvals} if tcol_total else None,
                         "patterns": pats}
            resumo["datasets"] += 1
            print(f"  [OK] {nome:40s} {len(pats)} padroes validados")
    json.dump(inv, open("_inventario_padroes.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\nRESUMO: {resumo}")
    print("inventario -> _inventario_padroes.json")


if __name__ == "__main__":
    main()
