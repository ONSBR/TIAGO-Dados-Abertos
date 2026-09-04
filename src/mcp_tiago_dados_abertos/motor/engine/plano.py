# -*- coding: utf-8 -*-
"""O resultado do planejamento (Plan) e a regra unica de somabilidade.

Parte do motor semantico; fatiado de semantics_engine.py pelas suas proprias secoes.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

# ── Resultado do planejamento ─────────────────────────────────────────────────

# v4 DESIGN: Whitelist FLOW_KINDS (explicit signal for summability)
# Only these quantity_kind values are inherently summable (flows, not stocks).
# Unknown kinds require explicit default_aggregation='sum' to be summable.
# POTENCIA (power*/average_power, MWmed) fica FORA de proposito: potencia soma
# espacialmente (subsistemas no mesmo instante) mas NAO atraves do tempo (somar
# MWmed de 12 meses e o erro classico de unidade), e o runtime nao distingue as
# duas formas. Melhor calar (mean cobre) do que somar errado.
FLOW_KINDS = frozenset({
    # Energy flows (MWh, GWh) - accumulate over time
    "energy",
    "energy_generation",
    "energy_consumption",
    "energy_flow",
    "energy_interchange",
    "energy_load",
    "energy_demand",
    # Volume flows
    "volume",
    "volume_flow",
    "water_flow",
    # Other flows
    "flow",
    "flux",
    # Fluxo FINANCEIRO (R$ que acumula: encargo, custo, montante, receita do
    # periodo). Distinto de price (R$/MWh, intensivo) e de financial_result/
    # value genericos (ambiguos, ficam fora ate curadoria especializar).
    "financial_flow",
})

# Kinds DECLARADOS que CONTRADIZEM soma: bloqueiam o Signal B (default_aggregation
# ='sum' sloppy no contrato NAO os torna somaveis). Nao e a blacklist antiga (que
# tornava somavel tudo fora dela SEM sinal); aqui ainda se exige default_agg='sum'
# explicito + modo SUM + nao-forbidden — esta lista so impede metadata contraditoria
# de vazar soma de estoque/preco/potencia-media.
# Unidades de MODO que denotam fluxo somavel (energia/volume acumulado). MW/MWmed
# (potencia) NAO entram: SUM sobre potencia e agregacao espacial, nao temporal.
_ENERGY_VOLUME_UNITS = frozenset({
    "kwh", "mwh", "gwh", "twh",
    "hm3", "hm³", "m3", "m³", "milm3", "milm³",
})

# Kinds duro-NAO-somaveis: veto GLOBAL — nenhum sinal (nem unit de energia no
# modo) os torna somaveis. Metadata com unit MWh num capacity e SUJA (ex.: 4
# contratos ccee com SUM(capacidade) nu rotulado kWh/MWh); vai pro worklist.
HARD_NO_SUM_KINDS = frozenset({
    "price",
    "energy_stock",
    "stock",
    "capacity",
    "ratio",
    "percent",
    "percentage",
    "level",
    "count",
    "currency",
    "area",
    "duration_minutes",
    # financeiros GENERICOS/ambiguos seguem duros; financial_flow (fluxo R$ que
    # acumula) foi promovido a FLOW_KINDS pela curadoria de ontologia.
    "financial",
    "financial_result",
    "financial_value",
})

# Kinds de POTENCIA: vetados do Signal B (default_agg sloppy nao basta), MAS o
# Signal A2 pode salva-los — caso legitimo: metrica MWmed cujo modo JA integra
# tempo no SQL (SUM(val*interval/1000) -> unit GWh).
POWER_KINDS = frozenset({"power", "average_power", "power_average"})

# Uniao usada pelo Signal B (nenhum dos grupos passa por default_aggregation).
CONTRADICTION_KINDS = HARD_NO_SUM_KINDS | POWER_KINDS


def _summable_core(metric: dict) -> Optional[bool]:
    """NUCLEO comum da regra de somabilidade (fonte unica).

    Compartilhado por _compute_summable (analisar) e metric_summable_for_sql
    (manual). Retorna True/False quando o nucleo decide sozinho, None quando a
    decisao depende do gate ESPECIFICO do caminho (A2-de-modo no analisar; nada
    no manual — None vira False la).
    - forbidden 'sum' -> False
    - HARD_NO_SUM_KINDS -> False (veto global)
    - FLOW_KINDS -> True
    - default_aggregation='sum' fora de CONTRADICTION_KINDS -> True (Signal B)
    - senao -> None (caminho decide: analisar tenta A2-do-modo; manual nega)
    """
    forbidden = metric.get("forbidden_aggregations") or []
    if isinstance(forbidden, list) and "sum" in [str(f).lower() for f in forbidden]:
        return False
    qkind = str(metric.get("quantity_kind", "") or "").lower()
    if qkind in HARD_NO_SUM_KINDS:
        return False
    # Revisao: 'volume' (FLOW_KINDS) tambem descreve ESTOQUE no corpus
    # (volumeutil em %, default last_or_avg) — somava 270%. Unidade percentual e veto
    # global; default_aggregation EXPLICITO diferente de sum (avg/last_or_avg/max) e a
    # declaracao do contrato de que a soma nao e o agregado natural: prevalece sobre o kind.
    unit = str(metric.get("unit_native") or metric.get("unit") or "").strip().lower()
    if unit in ("%", "percent", "pct"):
        return False
    default_agg = str(metric.get("default_aggregation", "") or "").lower()
    if default_agg and default_agg != "sum":
        return False
    if qkind in FLOW_KINDS:
        return True
    if default_agg == "sum" and qkind not in CONTRADICTION_KINDS:
        return True
    return None


def _compute_summable(mode: str, metric: Optional[dict]) -> bool:
    """Determine if the metric is summable for computed_facts.

    v4 DESIGN: Safe-by-default with WHITELIST (not blacklist).

    ALL of these must be true:
    1. mode uses SUM (aggregation_modes[mode].sql contains SUM), AND
    2. 'sum' is NOT in forbidden_aggregations, AND
    3. POSITIVE SIGNAL for accumulation:
       - quantity_kind is in FLOW_KINDS (explicit whitelist), OR
       - default_aggregation='sum' declared in the metric

    Unknown kinds (numeric, capacity, custom, etc.) are NOT summable by default.
    This is computed ONCE at plan-time using THE SELECTED metric, not re-looked up.
    """
    if not mode or not metric:
        return False

    # Rule 1: current mode must use SUM
    agg_modes = metric.get("aggregation_modes") or {}
    if mode not in agg_modes:
        return False

    entry = agg_modes[mode]
    if isinstance(entry, dict):
        sql = entry.get("sql", "")
    elif isinstance(entry, str):
        sql = entry
    else:
        return False

    if not sql or not re.search(r"\bSUM\s*\(", sql.upper()):
        return False

    # NUCLEO comum (fonte unica) + gate especifico deste caminho (A2-do-modo)
    core = _summable_core(metric)
    if core is not None:
        return core

    # Signal A2 (ESPECIFICO do analisar): a UNIDADE DO MODO e de energia/volume.
    # Cobre metrica de potencia nativa (MWmed) cujo modo JA converte pra energia
    # no proprio SQL declarado (SUM(val*interval/1000) -> GWh). So vale aqui
    # porque o SQL do MODO e conhecido/confiavel — no manual nao ha modo.
    unit_mode = ""
    if isinstance(entry, dict):
        unit_mode = str(entry.get("unit", "") or "").lower().replace(" ", "")
    if unit_mode in _ENERGY_VOLUME_UNITS:
        return True

    return False


def metric_summable_for_sql(metric: Optional[dict], executed_sql: str) -> bool:
    """Determina se a métrica é somável considerando o SQL EXECUTADO.

    v4.2 (Fatia 2): Função compartilhada entre analisar (via _compute_summable) e
    executar_sql (caminho manual). Fonte ÚNICA da regra de somabilidade.

    Combina:
    - Regras semânticas da métrica (quantity_kind, forbidden_aggregations, default_agg)
    - Gate no SQL EXECUTADO: SUM presente e AVG ausente

    Args:
        metric: Dicionário da métrica do contrato (pode ser None)
        executed_sql: SQL realmente executado

    Returns:
        True se é seguro somar as linhas do resultado, False caso contrário.
    """
    if not metric or not executed_sql:
        return False

    # Rule 1: SQL deve ter SUM e NÃO ter AVG
    sql_up = executed_sql.upper()
    if not re.search(r"\bSUM\s*\(", sql_up):
        return False
    if re.search(r"\bAVG\s*\(", sql_up):
        return False

    # NUCLEO comum (fonte unica). SEM Signal A2 aqui: A2 exige o SQL do MODO do
    # contrato (conversao declarada); num SQL manual nao ha modo — o caminho
    # manual e MAIS restrito por design (core None -> False).
    core = _summable_core(metric)
    return bool(core)


@dataclass
class Plan:
    """Plano de execucao deterministico derivado do contrato."""

    sql: Optional[str] = None
    clarify: Optional[str] = None
    redirect: Optional[str] = None
    mode: str = ""
    unit: str = ""
    pattern_name: str = ""
    params: dict = field(default_factory=dict)
    source: str = "semantics"  # semantics | fewshot | schema
    note: str = ""  # suposicao assumida (transparencia) quando a ambiguidade foi resolvida
    fallback_sql: Optional[str] = None  # SQL deterministico alternativo se o pattern falhar
    summable: bool = False  # v3.1: computed from THE SELECTED metric, not re-looked up


