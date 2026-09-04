# -*- coding: utf-8 -*-
"""Self-retrieval: mede se o contrato se recupera pela propria pergunta tipica.

Para cada contrato com typicalQuestions, usa a 1a pergunta como query, roda
rank_datasets (SEM filtro de orgao — cenario real) e ve em que posicao o dono
aparece. Baseline objetivo (top-1 / top-3). E o criterio de aceitacao
de qualquer refino de retrieval: enriquecer ragContext/vectorTags e ganho SO se
este numero sobe. Zero LLM.

Uso:
  python scripts/self_retrieval.py                 # tabela por orgao
  python scripts/self_retrieval.py --portal ons --misses    # lista os que falham
"""
import argparse
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mcp_tiago_dados_abertos.catalogo.catalog import load_all_contracts, rank_datasets  # noqa: E402


def _rank_of(catalog, key, query, top_n=10):
    # rank_datasets retorna [(score, name, meta), ...]; name e o nome PURO
    res = rank_datasets(catalog, query, top_n=top_n)
    alvo = key.split("/")[-1]
    for i, r in enumerate(res):
        name = r[1] if isinstance(r, (tuple, list)) and len(r) > 1 else str(r)
        if name == alvo:
            return i + 1
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--portal", default=None)
    ap.add_argument("--misses", action="store_true")
    args = ap.parse_args()

    catalog = load_all_contracts()
    stats = defaultdict(lambda: {"n": 0, "top1": 0, "top3": 0, "miss": 0})
    misses = []
    for key, meta in catalog.items():
        orgao = (meta.get("orgao") or "?").lower()
        if args.portal and orgao != args.portal.lower():
            continue
        # descontinuado NAO deve recuperar (o ranker o pula de proposito) — contar
        # como miss seria artefato da medicao; mede-se so o catalogo ATIVO.
        if meta.get("status") == "discontinued":
            continue
        tqs = [q for q in (meta.get("typical_questions") or []) if isinstance(q, str) and q.strip()]
        if not tqs:
            continue
        rank = _rank_of(catalog, key, tqs[0])
        s = stats[orgao]
        s["n"] += 1
        if rank == 1:
            s["top1"] += 1
        if rank and rank <= 3:
            s["top3"] += 1
        if not rank or rank > 3:
            s["miss"] += 1
            misses.append((key, tqs[0][:70], rank))

    print(f"{'portal':6s} | {'n':>4} | {'top1':>6} | {'top3':>6} | {'miss':>5}")
    print("-" * 42)
    for orgao, s in sorted(stats.items()):
        t1 = f"{100*s['top1']/s['n']:.0f}%" if s["n"] else "-"
        t3 = f"{100*s['top3']/s['n']:.0f}%" if s["n"] else "-"
        print(f"{orgao:6s} | {s['n']:>4} | {t1:>6} | {t3:>6} | {s['miss']:>5}")

    if args.misses:
        print("\n--- misses (dono fora do top-3) ---")
        for key, q, rank in misses[:40]:
            print(f"  [rank={rank}] {key.split('/')[-1][:45]}: {q}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
