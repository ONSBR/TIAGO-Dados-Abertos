# -*- coding: utf-8 -*-
"""Cast MEDIDO por coluna (nao por nome) -> sidecar revisavel, zero LLM.

Contexto: um refino automatico carimbou TRY_CAST(REPLACE(col, ',', '.') AS
DOUBLE) por PREFIXO de nome em 255 colunas. Dois defeitos provados:
  (1) o cast quebra separador de milhar ('1.234,56' -> '1.234.56' -> NULL);
  (2) mutou 315 arquivos (reflow YAML de 34k linhas, irrevisavel).

Aqui: le os `examples`/`enum` JA presentes no YAML cru, classifica o formato
REAL de cada coluna candidata, e escreve UM sidecar
`contracts/_measured_casts.json` (orgao/nome -> {coluna: cast}). O loader
sobrepoe em col['cast'] no carregamento. Nenhum contrato e reescrito -> diff
minimo e revisavel; a regeneracao do gerador nao regride (o sidecar e a camada
humana). Medicao classifica so quando ha CONSENSO nas amostras.

Cast por formato:
  pt-BR (virgula decimal, com/sem milhar) -> TRY_CAST(REPLACE(REPLACE(c,'.',''),',','.') AS DOUBLE)
  ponto-decimal cru ('1234.56')           -> NAO declara (ja parseavel; nunca remove ponto)
  ambiguo / sem amostra (>=2) / misto      -> NAO declara (omission-safe)

Uso:
  python scripts/measure_casts.py            # dry-run
  python scripts/measure_casts.py --write    # grava o sidecar
  python scripts/measure_casts.py --show
"""
import argparse
import glob
import json
import re
from collections import Counter
from pathlib import Path

import yaml

MCP = Path(__file__).resolve().parents[1]
CONTRACTS = MCP / "contracts"
SIDECAR = CONTRACTS / "_measured_casts.json"

_PTBR = re.compile(r"^-?\d{1,3}(?:\.\d{3})*,\d+$|^-?\d+,\d+$")
_DOTDEC = re.compile(r"^-?\d+\.\d+$")
_INT = re.compile(r"^-?\d+$")
_NUM_NAME = re.compile(r"vlr|val|qtd|mda|preco|potencia|energia|montante|tarifa|adicional|indice")

CAST_PTBR = "TRY_CAST(REPLACE(REPLACE({c}, '.', ''), ',', '.') AS DOUBLE)"


def _samples(prop: dict) -> list[str]:
    out = [str(x).strip() for x in (prop.get("examples") or [])]
    for cp in prop.get("customProperties") or []:
        if cp.get("property") == "enum":
            out += [str(v).strip() for v in (cp.get("value") or [])]
    return [x for x in out if x]


def _decide(prop: dict) -> str | None:
    """Retorna o cast pt-BR se a coluna e COMPROVADAMENTE vírgula-decimal; senao None.
    Consenso: >=2 amostras numericas, alguma pt-BR, e NENHUM ponto-decimal cru
    conflitante (misto = ambiguo = omite)."""
    name = (prop.get("name") or "").lower()
    if not _NUM_NAME.search(name):
        return None
    if prop.get("cast"):
        return None  # ja declarado
    s = _samples(prop)
    nums = [x for x in s if _PTBR.match(x) or _DOTDEC.match(x) or _INT.match(x)]
    if len(nums) < 2:
        return None
    ptbr = [x for x in nums if _PTBR.match(x)]
    dotdec = [x for x in nums if _DOTDEC.match(x) and not _PTBR.match(x)]
    if ptbr and not dotdec:
        return CAST_PTBR.format(c=prop.get("name"))
    return None


def build() -> dict:
    side: dict = {}
    for path in sorted(glob.glob(str(CONTRACTS / "*" / "*.odcs.yaml"))):
        p = Path(path)
        orgao, nome = p.parent.name, p.stem.replace(".odcs", "")
        try:
            d = yaml.safe_load(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        casts = {}
        for sch in d.get("schema") or []:
            for prop in sch.get("properties") or []:
                cast = _decide(prop)
                if cast:
                    casts[prop.get("name")] = cast
        if casts:
            side[f"{orgao}/{nome}"] = casts
    return side


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--show", action="store_true")
    args = ap.parse_args()

    side = build()
    por_portal = Counter()
    total = 0
    for key, casts in side.items():
        por_portal[key.split("/")[0]] += len(casts)
        total += len(casts)

    print("CASTS MEDIDOS (dado real dos examples; so pt-BR comprovado):\n")
    for p, n in sorted(por_portal.items()):
        print(f"  {p:6s}: {n} casts")
    print(f"\n  contratos: {len(side)} | total casts: {total}")

    if args.show:
        for key, casts in list(side.items())[:25]:
            for col, cast in casts.items():
                print(f"  {key.split('/')[-1]}.{col}")

    if args.write:
        SIDECAR.write_text(json.dumps(side, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        print(f"\n[write] {SIDECAR.relative_to(MCP)} ({total} casts)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
