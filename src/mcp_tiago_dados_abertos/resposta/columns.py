# -*- coding: utf-8 -*-
"""Reconhecimento de colunas do resultado pelo nome e pelo conteudo: temporal, relativa, ISO.

Usado pelo result-schema para atribuir papel a cada coluna do resultado.
"""

import re
import unicodedata

_TEMPORAL_COL_RE = re.compile(
    r"(?:^|_)(din|dat|data|date|periodo|mes|ano|dia|instante|competencia|hora|hor|time|timestamp|semana|week)(?:_|$)",
    re.I,
)
_RELATIVA_COL_RE = re.compile(r"(pct|percent|taxa|indice|fator|ratio|propor)", re.I)
_ISO_CELL_RE = re.compile(r"^\d{4}-\d{2}(?:-\d{2})?(?:[ T].*)?$")


def _sem_acento(s: str) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFD", s) if unicodedata.category(ch) != "Mn")


def _coluna_iso(rows: list, i: int) -> bool:
    """Coluna cujo conteudo e data/mes ISO (temporal por CONTEUDO, alias qualquer)."""
    vistos = 0
    for r in rows:
        if not isinstance(r, (list, tuple)) or len(r) <= i or r[i] is None:
            continue
        v = r[i]
        if hasattr(v, "isoformat"):
            vistos += 1
            continue
        if isinstance(v, str) and _ISO_CELL_RE.match(v.strip()):
            vistos += 1
            continue
        return False
    return vistos > 0
