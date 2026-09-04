# -*- coding: utf-8 -*-
"""Auditor de interval_hours por CONVENÇÃO de sufixo de pasta (datasets ONS S3).

O sufixo da pasta-fonte do ONS codifica o grão temporal, que fixa interval_hours:
  _tm = 30 min = 0.5h · _ho = 1h · _di = 24h · _me = 730h · _se = 168h · _an = 8760h

interval_hours ERRADO propaga direto para a conversão de energia (SUM*interval/1000=GWh):
foi a raiz do curtailment eólico sair 2x (declarava 1.0, fonte _tm = 0.5). Este checker
pega o mesmo defeito em contratos ATUAIS e FUTUROS. Rode no CI/guard de corpus.

Uso: python scripts/audit_interval_hours.py            (lista mismatches; exit 1 se houver)
     python scripts/audit_interval_hours.py --strict   (idem, para CI)
"""
import glob
import os
import re
import sys

import yaml

SUF = {"_tm": 0.5, "_ho": 1.0, "_di": 24.0, "_me": 730.0, "_se": 168.0, "_an": 8760.0}
# Datasets onde interval_hours NÃO dirige energia (índices/ratios %); mismatch é aviso, não erro.
SOFT = ("fator-capacidade", "ind_disponibilidade_", "ind_confiarb", "ind_qualid", "taxa_teif")


def _expected(loc: str):
    m = re.search(r"/dataset/([a-z0-9_\-]+)/", loc)
    folder = m.group(1) if m else ""
    for suf, h in SUF.items():
        if folder.endswith(suf):
            return h, folder
    return None, folder


def audit(contracts_dir: str = "contracts"):
    hard, soft = [], []
    for f in sorted(glob.glob(os.path.join(contracts_dir, "**", "*.odcs.yaml"), recursive=True)):
        try:
            d = yaml.safe_load(open(f, encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(d, dict):
            continue
        txt = open(f, encoding="utf-8").read()
        if "interval_hours" not in txt:
            continue
        loc = ""
        for s in (d.get("servers") or []):
            loc = s.get("location", "") or loc
        if "s3://" not in loc:  # só ONS S3 (convenção verificável)
            continue
        exp, folder = _expected(loc)
        if exp is None:
            continue
        vals = set(float(v) for v in re.findall(r"interval_hours['\":\s]+([0-9.]+)", txt))
        if vals and exp not in vals:
            base = os.path.basename(f)
            (soft if base.startswith(SOFT) or any(base.startswith(s) for s in SOFT) else hard).append(
                (base, sorted(vals), exp, folder)
            )
    return hard, soft


def main():
    hard, soft = audit()
    if hard:
        print(f"ERRO — interval_hours divergente da convenção (impacta energia): {len(hard)}")
        for base, vals, exp, folder in hard:
            print(f"  {base:50s} declara={vals} esperado={exp}  [{folder}]")
    if soft:
        print(f"AVISO — mismatch em índice/ratio (interval_hours não dirige energia): {len(soft)}")
        for base, vals, exp, folder in soft:
            print(f"  {base:50s} declara={vals} esperado={exp}  [{folder}]")
    if not hard and not soft:
        print("OK — interval_hours consistente com a convenção de sufixo em todos os datasets S3.")
    sys.exit(1 if hard else 0)


if __name__ == "__main__":
    main()
