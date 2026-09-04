# -*- coding: utf-8 -*-
"""Montagem de SQL a partir de metrics + dimensoes quando nenhum pattern serve; rollup SIN.

Parte do motor semantico; fatiado de semantics_engine.py pelas suas proprias secoes.
"""
from __future__ import annotations

import re
from typing import Optional

from mcp_tiago_dados_abertos.motor.pruning import prune_source, year_range_filter

from .ambiguidade import _question_has_granularity
from .normalizacao import _norm
from .parametros import _unit_of
from .patterns import resolver_fonte

# ── Fallback: montagem a partir de metrics + dimensoes ─────────────────────────

# Termos que indicam breakdown/comparacao por dimensao espacial — NAO aplica rollup
# Cobertura ampliada: variantes de "cada", "para cada", "entre", "compar".
# Termos base — deteccao de breakdown por dimensao declarada e dinamica.
# NAO inclui "entre os"/"entre as" genericos — sao falso positivo para
# intervalos temporais ("entre os anos 2022 e 2023"). A deteccao contextual
# "entre os/as <dimensao>" e feita dinamicamente em _question_asks_breakdown.
_BREAKDOWN_TERMS = (
    "por subsistema",
    "cada subsistema",
    "por regiao",
    "por região",
    "por região",
    "cada regiao",
    "cada região",
    "compar",
    "entre os subsistemas",
    "entre subsistemas",
    "para cada",
    # NAO inclui "entre os"/"entre as" genericos — falso positivo temporal
)


def _question_asks_breakdown(p_norm: str, meta: dict) -> bool:
    """Detecta breakdown de QUALQUER dimensao declarada em dimension_columns.

    Detecta "entre os/as <dimensao>" contextualmente (nao generico).
    Normaliza _ para espaco em intentos para matching de texto natural.

    Retorna True se a pergunta pede breakdown (por/cada/para cada/entre os/as) de qualquer
    dimensao declarada no contrato. Mantem termos espaciais existentes como subconjunto.
    """
    # 1. Termos estaticos (espaciais) — subconjunto existente
    for term in _BREAKDOWN_TERMS:
        if term in p_norm:
            return True

    # 2. Deteccao dinamica: "por <dim>", "cada <dim>", "para cada <dim>", "entre os/as <dim>"
    #    para qualquer dimensao declarada em semantics.dimension_columns (nome ou aliases)
    semantics = meta.get("semantics") or {}
    dim_cols = semantics.get("dimension_columns") or {}
    value_aliases = semantics.get("value_aliases") or {}
    metrics = semantics.get("metrics") or []

    # Coleta termos de dimensao: nome da dimensao + aliases conhecidos
    dim_terms: set[str] = set()
    for dim_name, col_value in dim_cols.items():
        # Nome da dimensao em si (ex.: "fonte", "subsistema", "usina")
        dim_terms.add(_norm(dim_name))
        # Se col_value e _pivot(...), extrai a metrica de breakdown
        if isinstance(col_value, str) and col_value.startswith("_pivot"):
            # Indica que a dimensao e decomposta em metricas por-fonte
            dim_terms.add(_norm(dim_name))

    # Adiciona aliases das dimensoes que aparecem em value_aliases
    for alias_key in value_aliases.keys():
        dim_terms.add(_norm(alias_key))

    # Padroes de breakdown: "por <dim>", "cada <dim>", "para cada <dim>", "entre os/as <dim>"
    for dim in dim_terms:
        if len(dim) < 3:
            continue
        # "por fonte", "por usina", "por subsistema", etc.
        if re.search(rf"\bpor\s+{re.escape(dim)}", p_norm):
            return True
        # "cada fonte", "cada usina"
        if re.search(rf"\bcada\s+{re.escape(dim)}", p_norm):
            return True
        # "para cada fonte"
        if re.search(rf"\bpara\s+cada\s+{re.escape(dim)}", p_norm):
            return True
        # "entre os <dim>", "entre as <dim>" — contextual, nao generico
        # Ex.: "entre os subsistemas" -> breakdown; "entre os anos" -> intervalo temporal (NAO e breakdown)
        if re.search(rf"\bentre\s+(?:os|as)\s+{re.escape(dim)}", p_norm):
            return True

    # 3. Metricas de breakdown por fonte: se a metrica selecionada tem intento por-fonte
    #    (ex.: geracao_por_fonte), nao colapsar
    # Normaliza _ para espaco para matching em texto natural
    for m in metrics:
        if not isinstance(m, dict):
            continue
        for intent in m.get("valid_intents") or []:
            intent_norm = _norm(str(intent))
            # Normaliza _ para espaco para matching de texto natural
            intent_norm_spaced = intent_norm.replace("_", " ")
            if "por_fonte" in intent_norm or "por fonte" in intent_norm:
                # Verifica se a pergunta menciona esse intento (com _ ou espaço)
                if intent_norm in p_norm or intent_norm_spaced in p_norm:
                    return True

    return False


def build_sin_rollup(
    pergunta: str,
    meta: dict,
    params: dict,
    metric: dict,
    mode: str,
) -> Optional[tuple[str, Optional[str]]]:
    """Constroi SQL de rollup SIN: primario = linha fisica SIN, fallback = rollup dos base.

    O SQL primario le a LINHA FISICA do total_member (ex. 'SIN'),
    que e o valor oficial ONS. O fallback (rollup soma dos base) so dispara se o
    primario vier vazio/erro.

    Suporta intervalos de ano (year_start/year_end) e bloqueia data sub-anual.

    Aplica-se quando:
    - meta["spatial_aggregate"] existe (opt-in via aggregateMember no contrato)
    - metric tem expression
    - pergunta e whole-system (sem filtro de membro base, sem breakdown)
    - mode mapeia para um redutor conhecido (average_power, peak_power, min, total_energy)
    - params["year"] ou params["year_start"/"year_end"] presente (sem ano -> None)
    - pergunta NAO pede granularidade temporal (diaria/mensal/anual/por mes/por dia)
    - params NAO tem marcadores de DATA sub-anual (start/end/data_inicio/etc) -> None

    Retorna (sql_primario, fallback_sql) ou None se nao se aplica.
    O fallback_sql presupõe métrica espacialmente aditiva; contratos futuros com
    métrica não-aditiva (ex. preço, percentual) devem declarar spatiallyAdditive=false
    antes de habilitar o fallback.
    """
    # Pre-requisito: spatial_aggregate declarado
    sp = meta.get("spatial_aggregate")
    if not sp or not isinstance(sp, dict):
        return None

    spatial_col = sp.get("column")
    total_member = sp.get("total_member")
    base_members = sp.get("base_members")
    if not spatial_col or not total_member or not base_members:
        return None

    # Pre-requisito: metrica com expression
    expr = metric.get("expression") if metric else None
    if not expr:
        return None

    # GATING - marcadores de DATA sub-anual -> None
    # Se o usuario especificou intervalo de datas (nao apenas anos), nao colapsar
    _subannual_markers = ("start", "end", "data_inicio", "data_fim", "start_date", "end_date")
    for marker in _subannual_markers:
        if params.get(marker) is not None:
            return None

    # Detectar modo temporal: year_start/year_end (intervalo) ou year (unico)
    year_start = params.get("year_start")
    year_end = params.get("year_end")
    year = params.get("year")
    use_year_range = year_start is not None and year_end is not None

    # GATING: sem ano E sem intervalo de anos -> None (evita varrer 2000-presente num escalar)
    if year is None and not use_year_range:
        return None

    p_norm = _norm(pergunta)

    # GATING: granularidade temporal pedida -> None (série granular tem pattern próprio)
    if _question_has_granularity(p_norm):
        return None

    # GATING - breakdown dimensional pedido (qualquer dimensao declarada) -> None
    if _question_asks_breakdown(p_norm, meta):
        return None

    # GATING: filtro de membro BASE -> não é whole-system
    # Compara APENAS a coluna espacial, não busca textual solta
    dim_filters = params.get("dimension_filters") or []
    non_spatial_filters: list[str] = []
    for flt in dim_filters:
        flt_lower = flt.lower()
        # Verifica se o filtro é na coluna espacial
        is_spatial_filter = spatial_col.lower() in flt_lower
        if is_spatial_filter:
            # Filtro para membro BASE -> não é whole-system
            for base in base_members:
                if f"'{base.lower()}'" in flt_lower or f"'{base}'" in flt:
                    return None
            # Filtro para o PROPRIO membro agregado (SIN) CONTA como whole-system — continua
        else:
            # Filtro NÃO-espacial: preservar no WHERE
            non_spatial_filters.append(flt)

    # Mapeamento de mode -> redutor SQL
    mode_reducers = {
        "average_power": "AVG",
        "peak_power": "MAX",
        "min": "MIN",
    }

    # Source - sem poda para intervalos multi-ano (prune_source so aceita 1 ano)
    source = meta.get("parquet_source") or meta.get("s3_location") or ""
    if not source:
        return None
    source = resolver_fonte(source, params)  # fonte de um-arquivo-por-periodo
    if not source:
        return None
    # Se intervalo de anos, usa o source original (glob); se ano unico, poda
    if not use_year_range:
        source = prune_source(source, meta.get("prune_partition"), year)
    # else: usa o glob original (source inalterado)

    # Coluna temporal
    semantics = meta.get("semantics") or {}
    row_grain = semantics.get("row_grain") or {}
    time_col = row_grain.get("temporal_column")
    if not time_col:
        return None

    # Filtro temporal - intervalo de anos ou ano unico
    where_parts: list[str] = []
    if use_year_range:
        # Intervalo: >= year_start-01-01 AND < (year_end+1)-01-01
        where_parts.append(
            f"TRY_CAST({time_col} AS TIMESTAMP) >= TIMESTAMP '{int(year_start)}-01-01' "
            f"AND TRY_CAST({time_col} AS TIMESTAMP) < TIMESTAMP '{int(year_end) + 1}-01-01'"
        )
    elif year:
        where_parts.append(year_range_filter(time_col, year))

    month = params.get("month")
    if month and time_col:
        where_parts.append(f"EXTRACT(MONTH FROM TRY_CAST({time_col} AS TIMESTAMP)) = {month}")

    # Adiciona filtros não-espaciais
    where_parts.extend(non_spatial_filters)

    # TRIM na coluna espacial: 2026 tem padding (ex. 'N  ', 'NE ', 'S  ', 'SE ')
    trim_col = f"TRIM(CAST({spatial_col} AS VARCHAR))"

    # Lista de membros base quoteada (para fallback)
    base_quoted = ", ".join(f"'{m}'" for m in base_members)

    metric_id = metric.get("id", "valor")

    # ─── Construir WHERE base (sem filtro espacial) ────────────────────────────
    where_base = " AND ".join(where_parts) if where_parts else ""

    # ─── PRIMARIO: filtro na linha física do total_member (SIN) ────────────────
    primary_spatial = f"{trim_col} = '{total_member}'"
    primary_where = f"{where_base} AND {primary_spatial}" if where_base else primary_spatial

    # ─── FALLBACK: rollup dos membros base (TRIM'd) ────────────────────────────
    fallback_spatial = f"{trim_col} IN ({base_quoted})"
    fallback_where = f"{where_base} AND {fallback_spatial}" if where_base else fallback_spatial

    # ─── total_energy: SUM flat ────────────────────────────────────────────────
    if mode == "total_energy":
        agg_modes = metric.get("aggregation_modes") or {}
        agg_sql = (agg_modes.get("total_energy") or {}).get("sql")
        if not agg_sql:
            # O fallback assumia passo de 1h por linha — em
            # dataset DIARIO (MWmed) a energia saia 24x menor. interval_hours
            # vem do contrato (metadado da metrica); SEM declaracao, OMITE
            # (prova-seguro-ou-omite: sintetizar conversao de energia com passo
            # chutado e a classe de erro, nao a solucao).
            ih = metric.get("interval_hours")
            if not isinstance(ih, (int, float)) or ih <= 0:
                return None
            agg_sql = f"SUM(({expr}) * {ih}) / 1000"

        primary_sql = f"SELECT {agg_sql} AS {metric_id} FROM {source} WHERE {primary_where}"
        fallback_sql = f"SELECT {agg_sql} AS {metric_id} FROM {source} WHERE {fallback_where}"
        return primary_sql, fallback_sql

    # ─── average_power / peak_power / min ──────────────────────────────────────
    reducer = mode_reducers.get(mode)
    if not reducer:
        return None

    # PRIMARIO: a linha SIN já é o total por instante, então AVG/MAX/MIN direto
    agg_modes = metric.get("aggregation_modes") or {}
    # tolerante a mode = string SQL direta OU objeto {sql}
    _e = agg_modes.get(mode)
    mode_sql = _e if isinstance(_e, str) else (_e or {}).get("sql")
    if not mode_sql:
        mode_sql = f"{reducer}({expr})"

    primary_sql = f"SELECT {mode_sql} AS {metric_id} FROM {source} WHERE {primary_where}"

    # FALLBACK: rollup aninhado (soma por instante sobre membros base)
    fallback_sql = (
        f"SELECT {reducer}(sin) AS {metric_id} "
        f"FROM (SELECT {time_col}, SUM({expr}) AS sin "
        f"FROM {source} WHERE {fallback_where} "
        f"GROUP BY {time_col})"
    )

    return primary_sql, fallback_sql


def _filter_fixes_spatial_single(flt: str, col: str) -> bool:
    """Prova ESTRUTURAL de que `flt` fixa `col` a exatamente UM literal.

    Usado pelo fix ÷4 para decidir NAO promover a espacial quando ela
    e constante. NAO usa _parse_filter: ele e permissivo no RHS (aceita
    `col = col` como valor e `IN ('SE', col)` extrai so os quoted -> cardinalidade
    1 falsa). Aqui: fullmatch + RHS obrigatoriamente literal
    (string quotada com escape '' ou numero); IN exige a lista INTEIRA = 1 literal.
    Qualquer coisa fora desses shapes -> False -> promove (fail-safe).
    """
    f = flt.strip().rstrip(";").strip()
    c = re.escape(col)
    lit = r"'(?:[^']|'')*'"
    # [0-9] explicito + re.ASCII: em Unicode, \d casa U+0661 (que o DuckDB le
    # como IDENTIFICADOR, nao numero) e IGNORECASE iguala U+0131 'ı' a 'i'
    # (ıd_subsistema ~ id_subsistema, colunas DISTINTAS no DuckDB).
    num = r"[0-9]+(?:\.[0-9]+)?"
    pats = (
        rf"TRIM\s*\(\s*CAST\s*\(\s*{c}\s+AS\s+VARCHAR\s*\)\s*\)\s*=\s*{lit}",
        rf"TRIM\s*\(\s*{c}\s*\)\s*=\s*{lit}",
        rf"{c}\s*=\s*{lit}",
        rf"{c}\s*=\s*{num}",
        rf"{c}\s+IN\s*\(\s*{lit}\s*\)",
    )
    return any(re.fullmatch(p, f, re.IGNORECASE | re.ASCII) for p in pats)


def build_from_metrics(pergunta: str, meta: dict, params: dict, metric: dict, mode: str) -> Optional[str]:
    """Monta SQL agregado a partir da metrica e dimensoes do contrato (sem pattern)."""
    semantics = meta.get("semantics") or {}
    source = meta.get("parquet_source") or meta.get("s3_location") or ""
    if not source or not metric:
        return None
    source = resolver_fonte(source, params)  # fonte de um-arquivo-por-periodo
    if not source:
        return None
    # Poda por ano SO com janela de ano unico (mesma guarda do pattern-path):
    # em intervalo multi-ano, podar pelo primeiro ano truncaria a parte recente.
    _bm_year = params.get("year")
    _bm_ys, _bm_ye = params.get("year_start"), params.get("year_end")
    if _bm_ys is not None and _bm_ye is not None and _bm_ys != _bm_ye:
        _bm_year = None
    source = prune_source(source, meta.get("prune_partition"), _bm_year)

    agg_modes = metric.get("aggregation_modes")
    if not isinstance(agg_modes, dict):
        agg_modes = {}
    # Tolerante a 2 formatos de aggregation_modes: objeto {sql: ...} OU
    # string SQL direta (alguns contratos CCEE: 'total_energy: SUM(...)'). Mesma
    # reconciliacao de formato do sourceExpression no catalog.
    _agg_entry = agg_modes.get(mode)
    agg_sql = _agg_entry if isinstance(_agg_entry, str) else (_agg_entry or {}).get("sql")
    # synthesized_avg: o agg foi SINTETIZADO aqui (fallback AVG), nao autoral do
    # contrato — e o unico caso onde o fix de reordenacao de dims se aplica.
    synthesized_avg = False
    if not agg_sql:
        cols = metric.get("columns") or [None]
        expr = metric.get("expression") or cols[0]
        if not expr:
            return None
        agg_sql = f"AVG({expr})"
        synthesized_avg = True
    metric_id = metric.get("id", "valor")

    row_grain = semantics.get("row_grain") or {}
    time_col = row_grain.get("temporal_column")
    dim_cols = semantics.get("dimension_columns") or {}
    # colunas reais (nao-nulas); ignora marcadores tipo _pivot(...) que nao sao colunas
    dims = [c for c in dim_cols.values() if c and not str(c).startswith("_") and "(" not in str(c)]

    # ── FIX ÷4: coluna espacial no GROUP BY do fallback AVG ────────
    # Se a espacial (ex.: id_subsistema) nao sobrevive ao corte dims[:2], o AVG
    # sintetizado varre as linhas de todos os subsistemas sem agrupar -> total/N
    # (o "÷4"). Defesa: reordenar a espacial para PRIMEIRO antes do slice.
    # - Escopo: SO quando o agg foi sintetizado (synthesized_avg) e mode nao e
    #   ranking — ranking agrupa pela entidade rankeada (ex. usina; reordenar a
    #   demoveria); modos com SQL explicito sao autorais do contrato.
    # - Excecao ESTRUTURAL (nao substring — colide com id_subsistema_jusante):
    #   se _filter_fixes_spatial_single PROVA (fullmatch + RHS literal) que um
    #   dimension_filter fixa a espacial a UM literal, ela e constante;
    #   promove-la desperdicaria uma vaga do dims[:2] e demoveria outra
    #   dimensao pedida. Multi-valor, OR composto, RHS nao-literal
    #   (`col = col`, `IN ('SE', col)`) PROMOVEM — fail-safe: na duvida, agrupa
    #   pela espacial.
    # - Identificacao: tupla ORDENADA de chaves sistemicas (set itera em ordem
    #   de hash, nao-deterministica entre processos). NAO usar row_grain.spatial
    #   cru: vale "usina" em 31 contratos e forcaria GROUP BY por usina
    #   (centenas de linhas) — fora do escopo desta correcao (colapso de
    #   subsistema).
    if synthesized_avg and mode != "ranking":
        spatial_col = None
        for key in ("subsistema", "submercado"):
            col = dim_cols.get(key)
            if col and isinstance(col, str) and not col.startswith("_") and "(" not in col:
                spatial_col = col
                break
        spatial_fixed_single = False
        if spatial_col:
            for flt in params.get("dimension_filters") or []:
                if _filter_fixes_spatial_single(flt, spatial_col):
                    spatial_fixed_single = True
                    break
        if spatial_col and spatial_col in dims and not spatial_fixed_single:
            dims = [spatial_col] + [d for d in dims if d != spatial_col]

    p_norm = _norm(pergunta)

    # ── Guarda de GRANULARIDADE DE LINHA (classe do erro "AVG por usina") ─────
    # AVG agrupado num grao mais GROSSO que a CHAVE DA LINHA (row_grain.
    # one_row_per) calcula a media POR LINHA — nao o agregado do grao pedido
    # (ex.: geracao-usina-2 tem ceg na chave; AVG por subsistema = media por
    # usina-hora, ~n_usinas x menor que a geracao regional). O sinal e a CHAVE
    # declarada no contrato, nao o rotulo entity (balanco tem entity=
    # fonte_agregada mas one_row_per=[din_instante, id_subsistema] — AVG por
    # subsistema la e EXATO). Coluna-chave fora do GROUP BY so passa se um
    # filtro de dimensao a fixa em um unico literal. SUM continua valido.
    _agg_head = str(agg_sql or "").lstrip().upper()
    if _agg_head.startswith("AVG("):
        _rg = semantics.get("row_grain") or {}
        _tcol = _rg.get("temporal_column")
        _grouped = set(dims[:2])  # dims[:2] = o que REALMENTE vai pro GROUP BY
        _filtros = params.get("dimension_filters") or []
        for _kc in _rg.get("one_row_per") or []:
            if not _kc or _kc == _tcol or _kc in _grouped:
                continue
            if not any(_filter_fixes_spatial_single(f, _kc) for f in _filtros):
                return None

    select, group, order = [], [], []

    # granularidade temporal
    trunc = None
    for kw, unit in [
        ("diari", "day"),
        ("por dia", "day"),
        ("mensal", "month"),
        ("por mes", "month"),
        ("anual", "year"),
        ("por ano", "year"),
        ("semanal", "week"),
    ]:
        if kw in p_norm:
            trunc = unit
            break
    if time_col and trunc:
        select.append(f"DATE_TRUNC('{trunc}', TRY_CAST({time_col} AS TIMESTAMP)) AS periodo")
        group.append("periodo")
        order.append("periodo")

    # dimensoes (limita a 2)
    for d in dims[:2]:
        select.append(d)
        group.append(d)

    select.append(f"{agg_sql} AS {metric_id}")
    # Participacao percentual DETERMINISTICA no breakdown por dimensao: aritmetica
    # derivada (o "X% do total") e do MOTOR, nao do LLM cliente — que comprovadamente
    # inventa percentuais quando precisa deriva-los da tabela.
    # Gates SEMANTICOS (por classe, contrato-dirigidos):
    #  - SO breakdown puro por dimensao (sem DATE_TRUNC): com agrupamento temporal o
    #    denominador OVER () varreria periodo x dimensao — share sem sentido;
    #  - SO grandeza com share significativo: agregacao SUM (aditiva) ou AVG de
    #    unidade absoluta. AVG de percentual/razao (EAR %, taxa, R$/MWh) NAO ganha
    #    share — "participacao de uma media de percentuais" e semanticamente falsa.
    _agg_up = (agg_sql or "").lstrip().upper()
    # _unit_of tolera mode = string SQL direta OU objeto {unit}
    _unit = str(_unit_of(agg_modes, mode, metric) or metric.get("unit_native") or "").lower()
    _qkind = str(metric.get("quantity_kind") or "").lower()
    # Grandeza JA-relativa nunca ganha share: quantity_kind ratio/percent/proporcao
    # (sinal declarado no contrato) ou unidade %, razao (/), proporcao, indice, pu.
    _relativa = any(t in _qkind for t in ("ratio", "percent", "propor")) or any(
        t in _unit for t in ("%", "/", "propor", "ratio", "indice", "adimensional", "pu")
    )
    _share_ok = not _relativa and (_agg_up.startswith("SUM(") or (_agg_up.startswith("AVG(") and bool(_unit)))
    if dims[:2] and trunc is None and _share_ok:
        select.append(f"ROUND(100.0 * ({agg_sql}) / NULLIF(SUM({agg_sql}) OVER (), 0), 1) AS participacao_pct")

    where = []
    year = params.get("year")
    _di, _df = params.get("data_inicio"), params.get("data_fim")
    _ws, _we = params.get("year_start"), params.get("year_end")
    if time_col and _di and _df:
        # Janela de datas EXATA (relativa resolvida ou intervalo ISO explicito).
        # Mais precisa que filtro de ano e cobre intervalo multi-ano. Fim sem
        # componente de hora ganha fim-de-dia inclusivo (coluna timestamp).
        _fim = _df if " " in str(_df) else f"{_df} 23:59:59"
        where.append(
            f"TRY_CAST({time_col} AS TIMESTAMP) >= TIMESTAMP '{_di}' "
            f"AND TRY_CAST({time_col} AS TIMESTAMP) <= TIMESTAMP '{_fim}'"
        )
    elif time_col and _ws is not None and _we is not None and _ws != _we:
        # Intervalo de anos literais sem datas exatas
        where.append(
            f"TRY_CAST({time_col} AS TIMESTAMP) >= TIMESTAMP '{int(_ws)}-01-01' "
            f"AND TRY_CAST({time_col} AS TIMESTAMP) < TIMESTAMP '{int(_we) + 1}-01-01'"
        )
    elif time_col and year and "year" in params:
        where.append(year_range_filter(time_col, year))
    if params.get("month") and time_col:
        where.append(f"EXTRACT(MONTH FROM TRY_CAST({time_col} AS TIMESTAMP)) = {params['month']}")
    for flt in params.get("dimension_filters") or []:
        where.append(flt)

    sql = f"SELECT {', '.join(select)} FROM {source}"
    if where:
        sql += f" WHERE {' AND '.join(where)}"
    if group:
        sql += f" GROUP BY {', '.join(group)}"
    if mode == "ranking":
        sql += f" ORDER BY {metric_id} DESC LIMIT {params.get('n', 10)}"
    elif order:
        sql += f" ORDER BY {', '.join(order)}"
    return sql


