# -*- coding: utf-8 -*-
"""Intencao de CONTAGEM (quantas usinas...) e a montagem do COUNT.

Parte do motor semantico; fatiado de semantics_engine.py pelas suas proprias secoes.
"""
from __future__ import annotations

import re
from typing import Optional

from mcp_tiago_dados_abertos.motor.pruning import prune_source, year_range_filter

from .normalizacao import _norm
from .patterns import resolver_fonte

# ── Deteccao de intencao COUNT ─────────────────────────────────────────────────

# Padroes que indicam intencao de CONTAGEM/existencia de entidades.
# NAO dispara para perguntas que pedem um valor metrico (ex.: "qual a geracao")
# nem para rankings comparativos (ex.: "quais subsistemas tem maior X").
_COUNT_POSITIVE = [
    # "quantos/quantas <entidade>"
    r"\bquant[oa]s\b",
    # "numero de / quantidade de"
    r"\bnumero de\b",
    r"\bquantidade de\b",
    # "quais <entidade> existem" / "quais sao os/as <entidade>"
    r"\bquais\b.{0,40}\bexistem\b",
    r"\bquais\b.{0,40}\bsao\s+(?:os|as)\b",
    # "liste / listar todos/as" (pedido de listagem completa ~ contagem)
    r"\bliste\b",
    r"\blistar\b",
    r"\blista\s+(?:todos?|todas?|os|as)\b",
]

# Padroes de EXCLUSAO: se a pergunta casa com estes, NAO e COUNT mesmo que
# contenha termos positivos. Evita falsos positivos.
# Exclusoes DURAS (sempre vencem): colapsar em COUNT escalar seria pior que baseline.
_COUNT_HARD_NEGATIVE = [
    # BREAKDOWN por dimensao (quer GROUP BY, nao escalar): ESTRUTURAL — qualquer
    # "por <substantivo>" indica agrupamento (por agente/nivel de tensao/estado/
    # subsistema...). Enquanto _build_count_sql nao faz GROUP BY, nao colapsar.
    # Exceto "por que"/"por ano" (temporal ja tratado) — filtra artigos/conjuncoes.
    r"\bpor\s+(?!que\b|a\b|o\b|as\b|os\b)\w+",
    # Contagem TEMPORAL/condicional (nao de entidade): "em quantos dias/vezes/meses"
    r"\bem\s+quant[oa]s\s+(?:dias?|vezes|meses|horas?|semanas?)\b",
    # ATRIBUTO/PROPRIEDADE da entidade (pede valor, nao contagem): ons_129, ons_136, ons_154
    r"\b(?:caracteristica|cota|volume|grandeza|parametro|atributo|propriedade)s?\b",
    # OBJETO METRICO / serie temporal:
    r"\b(?:mensal|diaria|horaria|serie)\b",
    r"\bvazao\b",
    r"\bm3/s\b",
    r"\bmwmed\b",
    r"\bmwh\b",
    # "modalidades"/"equipamentos ... de controle" = dimensao especifica, nao a
    # entidade default do contrato -> resolucao de entidade nao confiavel (ons_024/025)
    r"\bmodalidades?\b",
    r"\bequipamentos?\s+de\s+controle\b",
]

# Exclusoes SOFT: cedem a um positivo forte (quantos/existem/liste).
_COUNT_NEGATIVE = [
    # "qual a <metrica>" (pede valor, nao contagem)
    r"\bqual\s+(?:a|o)\s+\w+\b",
    # "quais <dim> tem maior/menor X" (ranking, nao count)
    r"\bquais\b.{0,40}\b(?:maior|menor|mais|menos|melhor|pior)\b",
    # Termos que indicam valor numerico / metrica
    r"\b(?:geracao|carga|demanda|energia|potencia|volume|nivel|tensao|capacidade)\s+(?:media|total|maxima|minima)\b",
]


def _detect_count_intent(p_norm: str) -> bool:
    """Detecta se a pergunta tem intencao de CONTAGEM de entidades.

    Retorna True apenas quando a pergunta pede existencia/quantidade de entidades,
    NAO quando pede um valor metrico ou ranking comparativo.
    """
    # Exclusoes DURAS: sempre vencem (mesmo com "quantos/existem/liste"). Grupo
    # por-dimensao quer GROUP BY (nao escalar); contagem TEMPORAL/condicional nao
    # e contagem de entidade; atributo/serie pede valor. Colapsar qualquer um em
    # COUNT(DISTINCT entidade) seria pior que o baseline.
    for hard in _COUNT_HARD_NEGATIVE:
        if re.search(hard, p_norm):
            return False

    # Exclusoes SOFT — "qual a X" cede a um positivo forte (quantos/existem/liste)
    for neg in _COUNT_NEGATIVE:
        if re.search(neg, p_norm):
            has_strong_positive = bool(
                re.search(r"\bquant[oa]s\b", p_norm)
                or re.search(r"\bnumero de\b", p_norm)
                or re.search(r"\bquantidade de\b", p_norm)
                or re.search(r"\bexistem\b", p_norm)
                or re.search(r"\bliste\b", p_norm)
            )
            if not has_strong_positive:
                return False

    for pos in _COUNT_POSITIVE:
        if re.search(pos, p_norm):
            return True
    return False


def _resolve_count_entity(meta: dict) -> tuple[str, str]:
    """Resolve a coluna de entidade a contar a partir do contrato.

    Retorna (coluna_para_count, label) — ex.: ("cod_equipamento", "linhas").
    Prioridade:
      1. Coluna nom_<entity> no schema (nome legivel da entidade, agrupa melhor)
      2. Coluna id_<entity> ou cod_<entity> em one_row_per (excluindo dimensoes)
      3. Coluna com prefixo cod_ em one_row_per (primary-key-like, nao dimensional)
      4. Primeira coluna nao-temporal nao-dimensional de one_row_per
      5. Coluna IDENTIFIER do schema
      6. COUNT(*)
    """
    semantics = meta.get("semantics") or {}
    row_grain = semantics.get("row_grain") or {}
    columns = meta.get("columns") or []

    one_row_per = row_grain.get("one_row_per") or []
    temporal_col = row_grain.get("temporal_column") or ""
    entity_label = row_grain.get("entity") or ""
    entity_norm = _norm(entity_label) if entity_label else ""

    # Colunas de one_row_per excluindo temporal
    candidates = [c for c in one_row_per if c != temporal_col]

    # Known dimensional columns that should NOT be the entity to count
    dim_cols = semantics.get("dimension_columns") or {}
    dim_col_names = {v for v in dim_cols.values() if v}

    # 1. nom_<entity> no schema (mais natural para contagens humanas)
    #    Ex.: entity="usina" -> "nom_usina"; entity="reservatorio" -> "nom_reservatorio"
    if entity_norm and len(entity_norm) >= 4:
        nom_target = f"nom_{entity_label}"
        nom_target_norm = _norm(nom_target)
        # Busca no schema (nome exato)
        for c in columns:
            if _norm(c.get("name", "")) == nom_target_norm:
                return c["name"], entity_label

    # 2. id_<entity> ou cod_<entity> em one_row_per (excluindo dimensoes)
    if entity_norm and len(entity_norm) >= 4:
        for c in candidates:
            if c in dim_col_names:
                continue
            c_norm = _norm(c)
            if entity_norm in c_norm:
                return c, entity_label

    # 3. Coluna com prefixo cod_ (strong identifier, not dimensional)
    for c in candidates:
        if c.startswith("cod_") and c not in dim_col_names:
            return c, entity_label or c

    # 4. Candidatos excluindo dimensoes conhecidas (id_subsistema etc)
    non_dim = [c for c in candidates if c not in dim_col_names]
    if non_dim:
        id_cols = [c for c in non_dim if c.startswith("id_")]
        if id_cols:
            return id_cols[0], entity_label or id_cols[0]
        return non_dim[0], entity_label or non_dim[0]

    # 5. Qualquer id_ em candidates
    id_cols = [c for c in candidates if c.startswith("id_")]
    if id_cols:
        return id_cols[0], entity_label or id_cols[0]

    if candidates:
        return candidates[0], entity_label or candidates[0]

    # 6. Coluna IDENTIFIER do schema que nao e temporal
    for c in columns:
        if c.get("semantic_type") == "IDENTIFIER" and c.get("name") != temporal_col:
            name = c["name"]
            if name not in dim_col_names:
                return name, entity_label or name

    # 7. Ultimo fallback: COUNT(*)
    return "*", entity_label or "registros"


def _build_count_sql(pergunta: str, meta: dict, params: dict) -> Optional[str]:
    """Constroi SQL de COUNT(DISTINCT entity) com filtros da pergunta."""
    source = meta.get("parquet_source") or meta.get("s3_location") or ""
    if not source:
        return None
    source = resolver_fonte(source, params)  # fonte de um-arquivo-por-periodo
    if not source:
        return None
    source = prune_source(source, meta.get("prune_partition"), params.get("year"))

    entity_col, _label = _resolve_count_entity(meta)

    if entity_col == "*":
        count_expr = "COUNT(*)"
    else:
        count_expr = f"COUNT(DISTINCT {entity_col})"

    where_clauses: list[str] = []

    # Filtros de dimensao extraidos via value_aliases (agora com stem-matching)
    for flt in params.get("dimension_filters") or []:
        where_clauses.append(flt)

    # Exclusao de entradas agregadas/grupo para COUNT de entidades individuais.
    exclusion = _count_exclusion_filter(meta)
    if exclusion:
        where_clauses.append(exclusion)

    # Filtro temporal (ano) se relevante
    semantics = meta.get("semantics") or {}
    row_grain = semantics.get("row_grain") or {}
    time_col = row_grain.get("temporal_column")
    year = params.get("year")
    if time_col and year and time_col not in ("dat_entradaoperacao",):
        # So aplica filtro temporal se o dataset e temporal (nao cadastral/STATIC)
        temporal_type = row_grain.get("temporal", "")
        if temporal_type and temporal_type != "STATIC":
            where_clauses.append(year_range_filter(time_col, year))

    sql = f"SELECT {count_expr} AS total FROM {source}"
    if where_clauses:
        sql += f" WHERE {' AND '.join(where_clauses)}"
    return sql


# Padroes nos nomes canonicos de value_aliases que indicam entradas "nao-individuais"
# (grupos agregados para previsao, sem medicao verificada). Exclui da contagem COUNT.
_AGGREGATE_PATTERNS = re.compile(
    r"Pequenas?\s+Usinas?",
    re.IGNORECASE,
)


def _count_exclusion_filter(meta: dict) -> Optional[str]:
    """Gera filtro NOT IN para excluir entradas agregadas/grupo da contagem.

    Percorre value_aliases buscando membros cujos nomes canonicos indicam
    agrupamento (ex.: 'Pequenas Usinas (MMGD)'). Se encontrar, retorna um
    filtro `col NOT IN ('val1', 'val2')`.
    """
    semantics = meta.get("semantics") or {}
    value_aliases = semantics.get("value_aliases") or {}

    for dim, members in value_aliases.items():
        if not isinstance(members, dict):
            continue
        exclusions: list[str] = []
        col_name = dim  # A dimensao do value_aliases e o nome da coluna
        for canon, info in members.items():
            if not isinstance(info, dict):
                continue
            if _AGGREGATE_PATTERNS.search(str(canon)):
                # Extrai o valor do filtro (ex.: "cod_modalidadeoperacao = 'Pequenas Usinas (MMGD)'")
                flt = info.get("filter", "")
                # Extrai valor entre aspas do filtro
                m = re.search(r"=\s*'([^']+)'", flt)
                if m:
                    exclusions.append(m.group(1))
        if exclusions:
            vals = ", ".join(f"'{v}'" for v in exclusions)
            return f"{col_name} NOT IN ({vals})"
    return None


