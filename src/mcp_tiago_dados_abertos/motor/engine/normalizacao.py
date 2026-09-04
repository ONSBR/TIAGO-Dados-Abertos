# -*- coding: utf-8 -*-
"""Normalizacao de texto: acentos, stem leve, casamento de frase.

Parte do motor semantico; fatiado de semantics_engine.py pelas suas proprias secoes.
"""
from __future__ import annotations

import re
import unicodedata

# ── Normalizacao ──────────────────────────────────────────────────────────────


def _norm(text: str) -> str:
    """Remove acentos e baixa caixa."""
    if not text:
        return ""
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii").lower()


def _stem(token: str) -> str:
    """Stem leve PT: remove plural e vogal final de genero (intradiario~intradiaria)."""
    t = token
    if len(t) > 4 and t.endswith("s"):
        t = t[:-1]
    if len(t) > 4 and t[-1] in "aoe":
        t = t[:-1]
    return t


def _phrase_match(p_norm: str, phrase: str) -> bool:
    """Casa um termo (mention/trigger) contra a pergunta normalizada.

    - Frases (com espaco): substring normalizada.
    - Palavra unica: casa por stem (tolera plural/genero), evitando falsos curtos.
    """
    ph = _norm(phrase).strip()
    if not ph:
        return False
    if " " in ph:
        return ph in p_norm
    stem = _stem(ph)
    if len(stem) < 4:  # token curto: exige match exato de palavra
        return re.search(rf"\b{re.escape(ph)}\b", p_norm) is not None
    return any(_stem(tok) == stem for tok in re.findall(r"\b\w+\b", p_norm))


