# -*- coding: utf-8 -*-
"""Selecao da metrica do contrato para a pergunta.

Parte do motor semantico; fatiado de semantics_engine.py pelas suas proprias secoes.
"""
from __future__ import annotations

import re
from typing import Optional

from .normalizacao import _norm

# ── Selecao de metrica ────────────────────────────────────────────────────────


def _select_metric(pergunta: str, semantics: dict) -> tuple[Optional[dict], int]:
    """Escolhe a metrica mais relevante. Retorna (metrica, score).

    score>0 indica match EXPLICITO (a pergunta nomeou intents/aliases da metrica);
    score==0 e fallback para a 1a metrica (nenhum match), p.ex. 'geracao total'.
    """
    metrics = semantics.get("metrics") or []
    if not metrics:
        return None, 0
    p_norm = _norm(pergunta)
    best = (0, metrics[0])
    for m in metrics:
        if not isinstance(m, dict):
            continue
        score = 0
        for token in list(m.get("valid_intents") or []) + list(m.get("aliases") or []):
            t = _norm(str(token))
            if t and re.search(rf"\b{re.escape(t)}\b", p_norm):
                score += 1
        if score > best[0]:
            best = (score, m)
    return best[1], best[0]


def _pattern_too_broad(sql: str, metric: dict, semantics: dict) -> bool:
    """O pattern agrega colunas de OUTRAS metricas, alem da metrica selecionada?

    Ex.: usuario pediu 'eolica' (metrica gereolica, col val_gereolica) mas caiu num
    pattern de geracao_total que soma val_gerhidraulica/termica/solar tambem.
    """
    sel = {c for c in (metric.get("columns") or []) if c}
    if not sel:
        return False
    foreign = {
        c
        for m in (semantics.get("metrics") or [])
        if isinstance(m, dict)
        for c in (m.get("columns") or [])
        if c and c not in sel
    }
    return any(re.search(rf"\b{re.escape(c)}\b", sql) for c in foreign)


