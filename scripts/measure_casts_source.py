# -*- coding: utf-8 -*-
"""Cast medido da FONTE REAL — colunas numericas VARCHAR SEM exemplo no contrato.

measure_casts.py cobre as colunas que tem examples no contrato (offline). Para as
numericas VARCHAR SEM amostra declarada, este script amostra a FONTE
(SELECT col LIMIT 30), classifica o formato REAL e faz MERGE no mesmo sidecar
_measured_casts.json. Zero LLM. Fonte que nao fetcha -> pula e loga (nao inventa cast).

Consenso identico ao offline: >=3 amostras nao-nulas, alguma pt-BR, nenhum
ponto-decimal cru conflitante -> cast double-replace; senao NAO declara.

Uso:
  python scripts/measure_casts_source.py --portal ons              # dry
  python scripts/measure_casts_source.py --portal ons --merge
"""
import argparse
import glob
import json
import re
from pathlib import Path

import duckdb
import yaml

MCP = Path(__file__).resolve().parents[1]
CONTRACTS = MCP / "contracts"
SIDECAR = CONTRACTS / "_measured_casts.json"

_PTBR = re.compile(r"^-?\d{1,3}(?:\.\d{3})*,\d+$|^-?\d+,\d+$")
_DOTDEC = re.compile(r"^-?\d+\.\d+$")
_INT = re.compile(r"^-?\d+$")
_NUM = re.compile(r"vlr|val|qtd|mda|preco|potencia|energia|montante|tarifa|adicional|indice|taxa|fator|percent")
CAST = "TRY_CAST(REPLACE(REPLACE({c}, '.', ''), ',', '.') AS DOUBLE)"


def _has_ex(prop) -> bool:
    ex = list(prop.get("examples") or [])
    for cp in prop.get("customProperties") or []:
        if cp.get("property") == "enum":
            ex += list(cp.get("value") or [])
    return len([x for x in ex if str(x).strip()]) >= 2


def _source(d) -> str | None:
    for cp in d.get("customProperties") or []:
        if cp.get("property") == "sourceExpression":
            return cp.get("value")
    for s in d.get("servers") or []:
        if s.get("location"):
            enc = "cp1252" if "aneel" in (s.get("location") or "") else "utf-8"
            return f"read_csv('{s['location']}', auto_detect=true, ignore_errors=true, encoding='{enc}')"
    return None


def _classify(vals: list[str]) -> str | None:
    nums = [v for v in vals if _PTBR.match(v) or _DOTDEC.match(v) or _INT.match(v)]
    if len(nums) < 3:
        return None
    ptbr = [v for v in nums if _PTBR.match(v)]
    dotdec = [v for v in nums if _DOTDEC.match(v) and not _PTBR.match(v)]
    return "ptbr" if (ptbr and not dotdec) else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--portal", default="ons")
    ap.add_argument("--merge", action="store_true")
    ap.add_argument("--timeout", type=int, default=60)
    args = ap.parse_args()

    novos = {}
    ok = falha = 0
    for path in sorted(glob.glob(str(CONTRACTS / args.portal / "*.odcs.yaml"))):
        p = Path(path)
        nome = p.stem.replace(".odcs", "")
        d = yaml.safe_load(p.read_text(encoding="utf-8"))
        if d.get("status") == "discontinued":
            continue
        alvos = []
        for sch in d.get("schema") or []:
            for prop in sch.get("properties") or []:
                nm = (prop.get("name") or "")
                pt = (prop.get("physicalType") or "").upper()
                if not _NUM.search(nm.lower()):
                    continue
                if pt and pt not in ("VARCHAR", "TEXT", "STRING", ""):
                    continue
                if any(cp.get("property") == "castExpression" for cp in prop.get("customProperties") or []):
                    continue
                if _has_ex(prop):
                    continue  # offline ja cobriu
                alvos.append(nm)
        if not alvos:
            continue
        src = _source(d)
        if not src:
            continue
        cols = ", ".join(f'"{c}"' for c in alvos)
        con = duckdb.connect()
        try:
            con.execute(f"SET http_timeout={args.timeout * 1000}")
            rows = con.execute(f"SELECT {cols} FROM {src} WHERE {alvos[0]} IS NOT NULL LIMIT 30").fetchall()
            ok += 1
        except Exception as e:  # noqa: BLE001
            print(f"[falha] {nome}: {str(e)[:70]}")
            falha += 1
            continue
        finally:
            con.close()
        casts = {}
        for i, c in enumerate(alvos):
            vals = [str(r[i]).strip() for r in rows if r[i] is not None]
            if _classify(vals):
                casts[c] = CAST.format(c=c)
        if casts:
            novos[f"{args.portal}/{nome}"] = casts
            print(f"[ok] {nome}: {list(casts.keys())}")

    total = sum(len(v) for v in novos.values())
    print(f"\nfontes lidas={ok} falhas={falha} | novos casts={total} em {len(novos)} contratos")

    if args.merge and novos:
        side = json.loads(SIDECAR.read_text(encoding="utf-8")) if SIDECAR.exists() else {}
        for k, casts in novos.items():
            side.setdefault(k, {}).update(casts)
        SIDECAR.write_text(json.dumps(side, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        print(f"[merge] sidecar atualizado: {sum(len(v) for v in side.values())} casts totais")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
