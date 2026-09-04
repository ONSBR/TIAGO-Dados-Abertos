# -*- coding: utf-8 -*-
"""A API do motor: plan(pergunta, meta) -> Plan.

Parte do motor semantico; fatiado de semantics_engine.py pelas suas proprias secoes.
"""
from __future__ import annotations

from .ambiguidade import _assumption_note, _default_assumption_for_mode, _resolved_mode_clarify, check_ambiguity
from .contagem import _build_count_sql, _detect_count_intent
from .fallback import build_from_metrics, build_sin_rollup
from .metricas import _pattern_too_broad, _select_metric
from .normalizacao import _norm
from .parametros import detect_mode, extract_params
from .plano import Plan, _compute_summable
from .template import render_sql_pattern

# ── API principal ─────────────────────────────────────────────────────────────


def plan(pergunta: str, meta: dict) -> Plan:
    """Produz um Plan deterministico para a pergunta sobre o dataset `meta`."""
    semantics = meta.get("semantics") or {}

    # 1. Ambiguidade / redirecionamento
    amb = check_ambiguity(pergunta, meta)
    if amb is not None:
        return amb

    # 2. Parametros + metrica + modo
    params = extract_params(pergunta, meta)

    # 2.5. Intencao COUNT — vence pattern metrico quando detectada
    p_norm = _norm(pergunta)
    if _detect_count_intent(p_norm):
        count_sql = _build_count_sql(pergunta, meta, params)
        if count_sql:
            return Plan(
                sql=count_sql,
                mode="count",
                unit="count",
                pattern_name="__count_intent__",
                params=params,
                source="semantics",
                note="Interpretado como contagem de entidades distintas.",
                summable=False,  # count is never summable
            )

    metric, metric_score = _select_metric(pergunta, semantics)
    metric = metric or {}
    mode, unit = detect_mode(pergunta, meta, metric) if metric else ("", "")

    # v3.1: compute summable ONCE using THE SELECTED metric
    summable = _compute_summable(mode, metric) if metric else False

    # Nota de transparencia: se uma clarify de modo/unidade foi resolvida pelo modo
    # explicito da pergunta, registra a suposicao (do default_assumptions do contrato).
    note = (
        _assumption_note(mode, _default_assumption_for_mode(meta, mode))
        if _resolved_mode_clarify(pergunta, meta)
        else ""
    )

    # 2.7. Rollup SIN: caminho deterministico para whole-system em datasets com aggregateMember
    # Primario = linha fisica SIN (valor oficial ONS), fallback = rollup dos base.
    # Vence sql_pattern quando se aplica — evita o bug "÷4" (media de breakdown vs escalar do SIN).
    sin_result = build_sin_rollup(pergunta, meta, params, metric, mode)
    if sin_result:
        primary_sql, fallback_sql = sin_result
        return Plan(
            sql=primary_sql,
            mode=mode,
            unit=unit,
            pattern_name="__sin_rollup__",
            params=params,
            source="semantics",
            note=note,
            fallback_sql=fallback_sql,
            summable=summable,
        )

    # 3. sql_pattern declarado (caminho preferencial)
    rendered = render_sql_pattern(pergunta, meta, params, mode)
    if rendered:
        sql, name = rendered
        # Se a pergunta nomeou uma metrica especifica (match explicito) mas o pattern
        # agrega outras fontes (ex.: 'eolica' caiu em geracao_total), monta a partir
        # da metrica selecionada — evita somar fontes que o usuario nao pediu.
        if metric_score > 0 and _pattern_too_broad(sql, metric, semantics):
            built = build_from_metrics(pergunta, meta, params, metric, mode)
            if built:
                return Plan(
                    sql=built,
                    mode=mode,
                    unit=unit,
                    pattern_name=metric.get("id", ""),
                    params=params,
                    source="semantics",
                    note=note,
                    summable=summable,
                )
        # Fallback deterministico: se o pattern do contrato falhar na execucao (ex.: GROUP BY
        # malformado), o analisar reexecuta com este build_from_metrics simples.
        fb = build_from_metrics(pergunta, meta, params, metric, mode) if metric else None
        return Plan(
            sql=sql,
            mode=mode,
            unit=unit,
            pattern_name=name,
            params=params,
            source="semantics",
            note=note,
            fallback_sql=(fb if fb and fb != sql else None),
            summable=summable,
        )

    # 4. Fallback: montar a partir de metrics + dimensoes
    if metric:
        sql = build_from_metrics(pergunta, meta, params, metric, mode)
        if sql:
            return Plan(sql=sql, mode=mode, unit=unit, params=params, source="schema", note=note, summable=summable)

    return Plan(params=params, mode=mode, unit=unit, summable=summable)
