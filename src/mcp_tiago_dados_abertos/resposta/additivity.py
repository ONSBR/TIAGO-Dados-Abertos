# -*- coding: utf-8 -*-
"""Aditividade de uma metrica pelo contrato: a soma de valores dessa metrica e legitima?

Fonte unica da regra, usada pelo result-schema (aditividade por coluna do resultado) e pelo
lineage (colunas cuja participacao e legitima). A decisao vem so do que o contrato declara:
agregacoes proibidas, `quantity_kind`, unidade e agregacao padrao.
"""

from typing import Optional

# Fluxos acumulam no tempo: somar faz sentido (energia em MWh, volume que passa, contagens).
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

# Estoques, precos, taxas e niveis: somar entre linhas nunca e um fato.
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

# Potencia media: so vira energia com o intervalo no SQL, decidido pelo AST, nunca pela coluna crua.
POWER_KINDS = frozenset({"power", "average_power", "power_average"})

# Kinds em que `default_aggregation: sum` NAO basta para provar soma.
CONTRADICTION_KINDS = HARD_NO_SUM_KINDS | POWER_KINDS


def metrica_somavel(metrica: dict) -> Optional[bool]:
    """True quando o contrato prova que somar e legitimo, False quando o contrato veta,
    None quando o contrato nao decide (o chamador trata None como nao provado).

    - `forbidden_aggregations` com `sum` -> False
    - `quantity_kind` em HARD_NO_SUM_KINDS -> False
    - unidade percentual -> False
    - `default_aggregation` explicito diferente de `sum` -> False (o contrato declarou que a
      soma nao e o agregado natural: 'volumeutil' em % com last_or_avg somava 270%)
    - `quantity_kind` em FLOW_KINDS -> True
    - `default_aggregation: sum` fora de CONTRADICTION_KINDS -> True
    - senao -> None
    """
    forbidden = metrica.get("forbidden_aggregations") or []
    if isinstance(forbidden, list) and "sum" in [str(f).lower() for f in forbidden]:
        return False
    qkind = str(metrica.get("quantity_kind", "") or "").lower()
    if qkind in HARD_NO_SUM_KINDS:
        return False
    unit = str(metrica.get("unit_native") or metrica.get("unit") or "").strip().lower()
    if unit in ("%", "percent", "pct"):
        return False
    default_agg = str(metrica.get("default_aggregation", "") or "").lower()
    if default_agg and default_agg != "sum":
        return False
    if qkind in FLOW_KINDS:
        return True
    if default_agg == "sum" and qkind not in CONTRADICTION_KINDS:
        return True
    return None
