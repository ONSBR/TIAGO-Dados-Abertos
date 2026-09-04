# -*- coding: utf-8 -*-
"""Ambiguidade e redirecionamento: quando pedir esclarecimento em vez de chutar.

Parte do motor semantico; fatiado de semantics_engine.py pelas suas proprias secoes.
"""
from __future__ import annotations

from typing import Optional

from .normalizacao import _norm, _phrase_match
from .parametros import _MODE_TRIGGERS
from .plano import Plan

# ── Ambiguidade e redirecionamento ────────────────────────────────────────────

# Termos que indicam que a pergunta JÁ especifica a granularidade temporal.
# Expandido para cobrir domínio completo: diária, horária, semanal, mensal, etc.
_GRANULARITY_TERMS = (
    "diari",
    "mensal",
    "anual",
    "semanal",
    "horari",  # horaria/horario
    "por dia",
    "por mes",
    "por ano",
    "por semana",
    "por hora",  # B: "por hora"
    "hora a hora",  # B: "hora a hora"
    "mes a mes",
    "trimestr",  # B: trimestral/trimestre
    "quinzenal",  # B: quinzenal
)


def _question_has_granularity(p_norm: str) -> bool:
    return any(t in p_norm for t in _GRANULARITY_TERMS)


def _is_granularity_clarify(question: str) -> bool:
    """A clarify pergunta a granularidade temporal (dia/mes/ano)?"""
    q = _norm(question)
    return ("dia" in q and "mes" in q and "ano" in q) or "granularidad" in q


def _explicit_mode(pergunta: str) -> Optional[str]:
    """Modo de agregacao EXPLICITAMENTE indicado na pergunta (sem fallback). None se ambiguo."""
    p_norm = _norm(pergunta)
    for mode, triggers in _MODE_TRIGGERS:
        if any(t.strip() in p_norm for t in triggers):
            return mode
    return None


_MODE_CLARIFY_HINTS = ("mwmed", "gwh", "mwh", "potencia", "energia total", "pico")


def _is_mode_clarify(question: str) -> bool:
    """A clarify pergunta o modo/unidade do resultado (MWmed vs GWh, pico, ...)?"""
    q = _norm(question)
    return any(h in q for h in _MODE_CLARIFY_HINTS)


def _default_assumption_for_mode(meta: dict, mode: str) -> Optional[dict]:
    """Suposicao-padrao (formato LISTA) cujo assume_output casa o modo. None se ausente/dict."""
    das = ((meta.get("semantics") or {}).get("ambiguity_policy") or {}).get("default_assumptions")
    if not isinstance(das, list) or not mode:
        return None
    for da in das:
        if isinstance(da, dict) and da.get("assume_output") == mode:
            return da
    return None


_MODE_LABELS = {
    "total_energy": "energia total (GWh)",
    "average_power": "potencia media (MWmed)",
    "peak_power": "pico (maximo)",
    "time_series": "serie temporal",
    "comparison": "comparacao",
    "ranking": "ranking",
}


def _assumption_note(mode: str, da: Optional[dict]) -> str:
    """Nota de transparencia sobre a interpretacao assumida (inclui a nota do contrato)."""
    base = f"Interpretado como {_MODE_LABELS.get(mode, mode)}. Refine a pergunta se quiser outra interpretacao."
    nota = (da.get("note") or "").strip() if isinstance(da, dict) else ""
    return f"{base} (contrato: {nota})" if nota else base


def _resolved_mode_clarify(pergunta: str, meta: dict) -> bool:
    """A pergunta dispararia uma clarify de modo/unidade, mas o modo foi explicitado?"""
    if not _explicit_mode(pergunta):
        return False
    p_norm = _norm(pergunta)
    policy = (meta.get("semantics") or {}).get("ambiguity_policy") or {}
    for rule in policy.get("must_clarify_when", []):
        if not isinstance(rule, dict):
            continue
        if any(_phrase_match(p_norm, str(t)) for t in (rule.get("trigger_terms") or [])):
            q = (rule.get("question") or "").strip()
            if q and _is_mode_clarify(q):
                return True
    return False


def check_ambiguity(pergunta: str, meta: dict) -> Optional[Plan]:
    """Aplica ambiguity_policy: redirect_when tem prioridade; depois must_clarify_when."""
    p_norm = _norm(pergunta)
    policy = (meta.get("semantics") or {}).get("ambiguity_policy") or {}

    # 1. Redirecionamentos explicitos (pergunta fora do escopo do dataset)
    for rule in policy.get("redirect_when", []):
        if not isinstance(rule, dict):
            continue
        if any(_phrase_match(p_norm, str(m)) for m in (rule.get("user_mentions") or [])):
            redirect = rule.get("redirect")
            reason = rule.get("reason", "")
            if redirect:
                return Plan(
                    redirect=(
                        f"Este dataset nao cobre o que voce pediu. {reason}\n\n"
                        f"**Dataset sugerido:** `{redirect}` — use `descrever_dataset('{redirect}')`."
                    )
                )

    # 2. Esclarecimentos obrigatorios — mas NAO pergunta granularidade temporal se a
    #    pergunta ja a especifica (ex.: "evolucao MENSAL ..." nao deve perguntar dia/mes/ano).
    has_gran = _question_has_granularity(p_norm)
    expl_mode = _explicit_mode(pergunta)
    for rule in policy.get("must_clarify_when", []):
        if not isinstance(rule, dict):
            continue
        if any(_phrase_match(p_norm, str(t)) for t in (rule.get("trigger_terms") or [])):
            q = (rule.get("question") or "").strip()
            if not q:
                continue
            if has_gran and _is_granularity_clarify(q):
                continue
            # usuario ja indicou o modo/unidade (ex.: "consumo total" -> energia total)
            if expl_mode and _is_mode_clarify(q):
                continue
            return Plan(clarify=q)
    return None


