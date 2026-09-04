# -*- coding: utf-8 -*-
"""Auditor de row_grain (dedup) — a chave declarada identifica a linha de verdade?

Verifica, no dado real, se `semantics.row_grain.one_row_per` DEDUP: COUNT(*) deve
igualar COUNT(DISTINCT one_row_per). Se COUNT(*) > distinct, a chave declarada e
GROSSA demais (ha varias linhas por chave) -> agregacoes por essa chave DOBRAM/
media-erram (classe do bug SIN/AVG). Amostra 1 ano recente por dataset (custo S3).

Uso: python scripts/audit_row_grain.py [dataset1 dataset2 ...]   (sem args = lista curada)
"""
import sys

import duckdb
import yaml

# Curada: series temporais ONS de energia (onde grao errado dobra valor).
CURADA = [
    "balanco-energia-subsistema", "carga-energia", "geracao-usina-2",
    "intercambio-nacional", "restricao_coff_eolica_usi", "curva-carga",
    "ear-diario-por-subsistema", "ena-diario-por-subsistema",
]

def _con():
    c = duckdb.connect(":memory:")
    c.execute("INSTALL httpfs; LOAD httpfs;")
    c.execute("SET s3_region='us-west-2'; SET s3_endpoint='s3.amazonaws.com';")
    c.execute("SET s3_url_style='path'; SET http_timeout=120000;")
    return c

def _load(nome):
    import glob
    for f in glob.glob(f"contracts/**/{nome}.odcs.yaml", recursive=True):
        d = yaml.safe_load(open(f, encoding="utf-8"))
        loc = ""
        for s in (d.get("servers") or []):
            loc = s.get("location", "") or loc
        sem = {}
        for p in (d.get("customProperties") or []):
            if p.get("property") == "semantics":
                sem = p.get("value") or {}
        rg = (sem.get("row_grain") or {}) if isinstance(sem, dict) else {}
        keys = rg.get("one_row_per") or []
        tcol = rg.get("temporal_column") or ""
        return loc, keys, tcol
    return None, [], ""

def audit(nome, con):
    loc, keys, tcol = _load(nome)
    if not loc or "s3://" not in loc or not keys:
        return (nome, "SKIP", "sem s3/row_grain")
    src = f"read_parquet('{loc}', union_by_name=true)"
    key_expr = ", ".join(f"TRY_CAST({k} AS VARCHAR)" for k in keys)
    where = ""
    if tcol:
        where = f"WHERE EXTRACT(YEAR FROM TRY_CAST({tcol} AS TIMESTAMP)) = 2025"
    q = f"SELECT COUNT(*) n, COUNT(DISTINCT ({key_expr})) d FROM {src} {where}"
    try:
        n, d = con.execute(q).fetchone()
    except Exception as e:
        return (nome, "ERRO", str(e)[:80])
    if n == 0:
        return (nome, "VAZIO", "sem dados 2025")
    dup = n - d
    status = "OK" if dup == 0 else "DUP"
    return (nome, status, f"linhas={n} distinct={d} dups={dup} ({100*dup/n:.1f}%) keys={keys}")

def main():
    alvos = sys.argv[1:] or CURADA
    con = _con()
    print(f"auditoria row_grain (amostra 2025) — {len(alvos)} datasets\n")
    for nome in alvos:
        r = audit(nome, con)
        print(f"  [{r[1]:5s}] {r[0]:36s} {r[2]}")

if __name__ == "__main__":
    main()
