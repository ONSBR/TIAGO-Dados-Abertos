# -*- coding: utf-8 -*-
"""Auditor de cobertura DEFASADA em fonte viva (guard continuo).

Detecta contratos que declaram periodoCoberturaFim mas cujos dados ja passaram
dessa data — a instrucao "verifique a cobertura antes de filtrar" induziria
RECUSA HONESTA FALSA em perguntas mais recentes.

Regra: candidato = contrato com periodoCoberturaFim >= um piso recente (default
2026-01-01; encerrado de verdade fica intacto) e fora de 'descontinuado'. Para
cada candidato, mede MAX(coluna temporal) na fonte VIVA via DuckDB. Medido >
declarado = cobertura defasada -> com --apply, REMOVE o periodoCoberturaFim do
YAML (ausencia = 'ate o presente' na regra da cobertura honesta do
descrever_dataset).

Uso:
  python scripts/audit_coverage_stale.py            # so relata
  python scripts/audit_coverage_stale.py --apply    # remove Fim dos defasados
"""
import argparse
import concurrent.futures as cf
import re
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import duckdb  # noqa: E402

from mcp_tiago_dados_abertos.catalogo.catalog import load_all_contracts  # noqa: E402

CONTRACTS_DIR = Path(__file__).resolve().parents[1] / "contracts"
PROBE_TIMEOUT_S = 45


def _temporal_col(meta: dict) -> str | None:
    sem = meta.get("semantics") or {}
    rg = sem.get("row_grain") or {}
    col = rg.get("temporal_column")
    if col:
        return col
    for c in meta.get("columns") or []:
        t = (c.get("physicalType") or c.get("type") or "").upper()
        if t in ("DATE", "TIMESTAMP"):
            return c.get("name")
    # Fallback por NOME (meta sem tipo declarado no catalogo): one_row_per
    # primeiro (grao declarado), depois qualquer coluna Dat*/din_*
    for c in rg.get("one_row_per") or []:
        if re.match(r"(?i)^(dat|din_|dt_)", str(c)):
            return str(c)
    for c in meta.get("columns") or []:
        if re.match(r"(?i)^(dat|din_|dt_)", c.get("name") or ""):
            return c.get("name")
    return None


def _probe_max(source: str, col: str) -> str | None:
    con = duckdb.connect()
    try:
        con.execute("SET http_timeout=40000")
        row = con.execute(
            f"SELECT MAX(TRY_CAST({col} AS DATE)) FROM {source}"
        ).fetchone()
        return str(row[0]) if row and row[0] else None
    finally:
        con.close()


def _normaliza_fim(fim: str) -> str:
    """'2026' -> '2026-12-31'; '2026-05' -> '2026-05-31' (comparacao conservadora:
    so acusa defasado se o medido passar do fim MAIS GENEROSO da declaracao)."""
    if re.fullmatch(r"20\d{2}", fim):
        return f"{fim}-12-31"
    if re.fullmatch(r"20\d{2}-\d{2}", fim):
        return f"{fim}-28"
    return fim[:10]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--piso", default="2026-01-01", help="so audita Fim >= piso")
    args = ap.parse_args()

    catalog = load_all_contracts()
    candidatos = []
    for key, meta in catalog.items():
        fim = (meta.get("period_end") or "").strip()
        nome = meta.get("name") or key
        if not fim or "descontinuado" in nome:
            continue
        if _normaliza_fim(fim) < args.piso:
            continue
        col = _temporal_col(meta)
        src = meta.get("parquet_source") or ""
        if not col or not src:
            print(f"[skip] {nome}: sem coluna temporal/fonte")
            continue
        candidatos.append((key, nome, fim, col, src))

    print(f"{len(candidatos)} candidatos (Fim >= {args.piso})\n")
    defasados = []
    with cf.ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(_probe_max, src, col): (key, nome, fim) for key, nome, fim, col, src in candidatos}
        for fut in cf.as_completed(futs):
            key, nome, fim = futs[fut]
            try:
                medido = fut.result(timeout=PROBE_TIMEOUT_S)
            except Exception as e:  # noqa: BLE001
                print(f"[erro] {nome}: {type(e).__name__}: {str(e)[:90]}")
                continue
            if not medido:
                print(f"[vazio] {nome}")
                continue
            if medido[:10] > _normaliza_fim(fim):
                defasados.append((key, nome, fim, medido[:10]))
                print(f"[DEFASADO] {nome}: declarado ate {fim}, medido {medido[:10]}")
            else:
                print(f"[ok] {nome}: {fim} (medido {medido[:10]})")

    print(f"\n{len(defasados)} defasados")
    if not args.apply or not defasados:
        return 0

    hoje = date.today().isoformat()
    for key, nome, fim, medido in defasados:
        matches = list(CONTRACTS_DIR.rglob(f"{nome}.odcs.yaml"))
        if len(matches) != 1:
            print(f"[apply-skip] {nome}: {len(matches)} arquivos")
            continue
        p = matches[0]
        txt = p.read_text(encoding="utf-8")
        # bloco '- property: periodoCoberturaFim\n  value: ...' -> removido
        novo, n = re.subn(
            r"- property: periodoCoberturaFim\n\s+value: '?[0-9-]+'?\n",
            "",
            txt,
            count=1,
        )
        if n != 1:
            print(f"[apply-skip] {nome}: bloco nao casou ({n})")
            continue
        p.write_text(novo, encoding="utf-8")
        print(f"[apply] {nome}: Fim '{fim}' removido (medido {medido}, {hoje})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
