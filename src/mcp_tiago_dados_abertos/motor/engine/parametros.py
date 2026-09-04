# -*- coding: utf-8 -*-
"""Extracao de parametros da pergunta e predicado canonico dos filtros (ciclo mutuo, por isso um arquivo).

Parte do motor semantico; fatiado de semantics_engine.py pelas suas proprias secoes.
"""
from __future__ import annotations

import datetime as _dt
import re
from typing import Optional

from mcp_tiago_dados_abertos.motor.context import resolve_temporal_expressions

from .normalizacao import _norm, _phrase_match

# ── Extracao de parametros ────────────────────────────────────────────────────

_MESES = {
    "janeiro": 1,
    "fevereiro": 2,
    "marco": 3,
    "abril": 4,
    "maio": 5,
    "junho": 6,
    "julho": 7,
    "agosto": 8,
    "setembro": 9,
    "outubro": 10,
    "novembro": 11,
    "dezembro": 12,
}

# Modos de agregacao mapeados por intencao da pergunta (em ordem de prioridade)
_MODE_TRIGGERS = [
    (
        "total_energy",
        {"energia total", "consumo total", "total consumid", "gwh", "twh", "mwh", "energia gerada", "geracao total"},
    ),
    ("peak_power", {"pico", "maxima", "maximo", "maior valor", "recorde", "demanda maxima"}),
    ("ranking", {"ranking", "top ", "maiores", "menores", "lista as", "quais usinas", "principais"}),
    ("comparison", {"compar", "versus", " vs ", "diferenca", "entre os subsistemas"}),
    (
        "time_series",
        {
            "evolucao",
            "serie",
            "ao longo",
            "historico",
            "tendencia",
            "mes a mes",
            "por mes",
            "mensal",
            "por ano",
            "por dia",
            "diaria",
        },
    ),
    ("average_power", {"media", "medio", "carga media", "demanda media", "mwmed", "potencia media", "em mwmed"}),
]


def extract_params(pergunta: str, meta: dict) -> dict:
    """Extrai ano, mes, intervalo de datas e filtros de dimensao via value_aliases."""
    p_norm = _norm(pergunta)
    params: dict = {}

    # Ano (explicito) — senao deixa o caller decidir o default.
    # Lookahead negativo: numero seguido de unidade ("2000 MW", "1500 MWmed")
    # e VALOR, nao ano — sem isso bloquearia a resolucao de janela relativa.
    anos = re.findall(
        r"\b(19\d{2}|20\d{2})\b"
        r"(?!\s*(?:mw|gw|kw|tw|mva|kv|mwh|gwh|twh|mwmed|mwm|megawatts?|gigawatts?|m3/s|m3|hm3|m³|%))",
        pergunta,
        flags=re.IGNORECASE,
    )
    if anos:
        params["year"] = int(anos[0])
        if len(anos) >= 2:
            params["year_start"] = int(anos[0])
            params["year_end"] = int(anos[-1])

    # Mes — match por palavra inteira (evita "maio" casar "maiores"/"maior")
    for nome, num in _MESES.items():
        if re.search(rf"\b{nome}\b", p_norm):
            params["month"] = num
            break

    # Datas pt-BR (DD/MM/AAAA): sem isto, de "no dia 01/09/2026" so sobrava o
    # ANO e o pattern mensal respondia o ano inteiro. Ano de 2 digitos e "01/09" sem ano NAO
    # viram data (ambiguo: nao inventa).
    datas_br = []
    for d, m, a in re.findall(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", pergunta):
        try:
            datas_br.append(_dt.date(int(a), int(m), int(d)).isoformat())
        except ValueError:
            continue  # 32/13/2026 e lixo, nao data

    # Datas ISO explicitas (YYYY-MM-DD)
    datas = re.findall(r"\b(\d{4}-\d{2}-\d{2})\b", pergunta) or datas_br
    if len(datas) >= 2:
        params["data_inicio"], params["data_fim"] = datas[0], datas[-1]
        params["start_date"], params["end_date"] = datas[0], datas[-1]
        params["start"], params["end"] = datas[0], datas[-1]
        params["data"] = datas[-1]
    elif len(datas) == 1:
        # DIA UNICO: a janela e o proprio dia (fim inclusivo em coluna timestamp)
        dia = datas[0]
        params["data"] = dia
        params["data_inicio"] = params["start_date"] = params["start"] = dia
        params["data_fim"] = params["end_date"] = params["end"] = f"{dia} 23:59:59"
    for d in datas[:1]:
        params.setdefault("year", int(d[:4]))
        params.setdefault("month", int(d[5:7]))

    # Janelas RELATIVAS ("ontem", "ultimos 7 dias", "semana passada"...): sem nada
    # temporal literal na pergunta, resolve as expressoes relativas para datas ISO
    # e extrai da versao reescrita. Sem isso os placeholders temporais dos patterns
    # caem em validated_params do contrato (datas de amostra da validacao, ex.
    # 2000-01-01) e o SQL filtra o periodo errado.
    if not any(k in params for k in ("year", "month", "data_inicio")):
        temporal = resolve_temporal_expressions(pergunta)
        if temporal.notes:
            datas_rel = re.findall(r"\b(\d{4}-\d{2}-\d{2})\b", temporal.rewritten)
            if datas_rel:
                ini, fim = datas_rel[0], datas_rel[-1]
                params["data_inicio"] = params["start_date"] = params["start"] = ini
                # Fim-de-dia inclusivo: em coluna timestamp, 'YYYY-MM-DD' cru
                # cobriria so a meia-noite do ultimo dia da janela.
                fim_dia = f"{fim} 23:59:59"
                params["data_fim"] = params["end_date"] = params["end"] = fim_dia
                # Pattern de data unica responde o dia mais RECENTE da janela.
                params["data"] = fim
                # year so quando a janela NAO cruza ano: a poda por particao
                # (prune_sql) nao tem guarda multi-ano e truncaria a parte
                # recente da janela (justo a que a pergunta pede).
                if ini[:4] == fim[:4]:
                    params["year"] = int(ini[:4])

    # Filtros de dimensao via value_aliases do contrato
    filters = _resolve_dimension_filters(p_norm, meta)
    if filters:
        params["dimension_filters"] = filters

    return params


# Stopwords PT que coincidem com codigos de dimensao (ex.: "se" ~ Sudeste).
# Nao devem disparar filtro por si sos (evita "...se houver..." -> id_subsistema='SE').
_PT_STOPWORDS = {
    "se",
    "de",
    "do",
    "da",
    "no",
    "na",
    "em",
    "os",
    "as",
    "ao",
    "ou",
    "e",
    "a",
    "o",
    "um",
    "uma",
    "que",
    "com",
    "por",
    "para",
    "dos",
    "das",
    "nos",
    "nas",
    "sem",
    "mais",
    "ate",
    "entre",
    "sobre",
    "como",
}

# Tipos fisicos que NAO sao textuais (nao devem receber TRIM)
_NON_TEXTUAL_TYPES = frozenset(
    {
        "BOOLEAN",
        "INTEGER",
        "BIGINT",
        "SMALLINT",
        "TINYINT",
        "DOUBLE",
        "FLOAT",
        "REAL",
        "DECIMAL",
        "NUMERIC",
        "DATE",
        "TIMESTAMP",
        "TIME",
        "INTERVAL",
        "BLOB",
        "BYTEA",
    }
)


def _build_col_index(meta: dict) -> dict[str, dict]:
    """Constroi indice nome_coluna -> {type, logical, semantic_type} a partir de meta['columns']."""
    cols = meta.get("columns") or []
    index = {}
    for c in cols:
        name = c.get("name")
        if name:
            index[name] = {
                "type": (c.get("type") or "").upper(),
                "logical": c.get("logical", ""),
                "semantic_type": c.get("semantic_type", ""),
            }
    return index


def _is_textual_column(col_name: str, col_index: dict[str, dict]) -> bool:
    """Determina se uma coluna e textual (deve receber TRIM).

    Regra:
    - physicalType tem PRECEDENCIA: se for um tipo nao-textual (_NON_TEXTUAL_TYPES), retorna False
    - TEXTUAL = type == "VARCHAR" OU (type ausente/indeterminado E logical == "string")
    """
    info = col_index.get(col_name)
    if not info:
        # Coluna desconhecida: assume textual por seguranca (TRIM nao quebra numeros)
        return True
    phys_type = info.get("type", "")
    logical = info.get("logical", "")

    # Se tipo fisico e conhecido e nao-textual, NAO aplica TRIM
    if phys_type and phys_type in _NON_TEXTUAL_TYPES:
        return False

    # Se tipo fisico e VARCHAR, e textual
    if phys_type == "VARCHAR":
        return True

    # Se tipo fisico ausente/indeterminado e logical == "string", e textual
    if not phys_type and logical == "string":
        return True

    # Caso default: nao e textual
    return False


def _escape_sql_value(value: str) -> str:
    """Escapa aspas simples em valores SQL: ' -> ''.

    Se o valor ja contem escape correto (''), nao re-escapa.
    """
    # Se ja tem '' (escape), assume que ja esta escapado
    if "''" in value:
        return value
    return value.replace("'", "''")


def _is_scalar_literal(filter_str: str) -> bool:
    """Verifica se um filtro contem apenas literais escalares ATOMICOS.

    Retorna False para:
    - Expressoes com funcoes (MAX, MIN, AVG, COUNT, etc)
    - Window functions (OVER, PARTITION BY)
    - Subqueries (SELECT)
    - Operadores aritmeticos complexos
    - Conectores logicos (OR, AND) — filtros compostos
    - IS NULL / IS NOT NULL — expressoes nao-EQ/IN

    Filtros compostos devem ser preservados intactos.
    """
    flt_upper = filter_str.upper()
    # Funcoes agregadas ou de janela
    if re.search(r"\b(MAX|MIN|AVG|SUM|COUNT|OVER|PARTITION|SELECT)\s*\(", flt_upper):
        return False
    # Subquery
    if re.search(r"\(\s*SELECT\b", flt_upper):
        return False
    # Conectores logicos indicam filtro composto — preservar intacto
    if re.search(r"\b(OR|AND)\b", flt_upper):
        return False
    # IS NULL / IS NOT NULL — nao e EQ/IN atomico
    if re.search(r"\bIS\s+(NOT\s+)?NULL\b", flt_upper):
        return False
    return True


def _parse_filter(filter_str: str) -> Optional[tuple[str, str, list[str]]]:
    """Parseia um filtro em (coluna, operador, valores).

    Suporta formatos:
    - col = 'val'
    - col = 'val with ''escape'''
    - col = val (sem aspas)
    - TRIM(col) = 'val'
    - TRIM(CAST(col AS VARCHAR)) = 'val'
    - col IN ('a', 'b')

    IMPORTANTE: Retorna None para filtros com expressoes complexas (funcoes, subqueries, etc)
    para que sejam preservados como estao.

    Retorna (col_name, operador, [valores]) ou None se nao conseguir parsear.
    """
    flt = filter_str.strip()

    # FALHA 4: Expressoes complexas nao devem ser parseadas — retornam None
    # para serem preservadas intactas
    if not _is_scalar_literal(flt):
        return None

    # Regex para valor entre aspas que suporta escape '' (apostrofo duplo)
    # Captura tudo entre aspas simples, incluindo '' como escape
    # Ex: 'O''Connor' -> O''Connor
    quoted_val = r"'((?:[^']|'')*)'"

    # Tenta casar: TRIM(...col...) = 'val' ou col = 'val'
    # Padroes mais especificos primeiro

    # Pattern 1: TRIM(CAST(col AS VARCHAR)) = 'val'
    m = re.match(
        rf"TRIM\s*\(\s*CAST\s*\(\s*(\w+)\s+AS\s+VARCHAR\s*\)\s*\)\s*=\s*{quoted_val}",
        flt,
        re.IGNORECASE,
    )
    if m:
        return (m.group(1), "=", [m.group(2)])

    # Pattern 2: TRIM(col) = 'val'
    m = re.match(rf"TRIM\s*\(\s*(\w+)\s*\)\s*=\s*{quoted_val}", flt, re.IGNORECASE)
    if m:
        return (m.group(1), "=", [m.group(2)])

    # Pattern 3: col IN ('a', 'b', ...)
    m = re.match(r"(\w+)\s+IN\s*\(\s*(.+?)\s*\)", flt, re.IGNORECASE)
    if m:
        col = m.group(1)
        vals_str = m.group(2)
        # Extrai valores entre aspas (suportando escape '')
        vals = re.findall(r"'((?:[^']|'')*)'", vals_str)
        if vals:
            return (col, "IN", vals)

    # Pattern 4: col = 'val' (com aspas)
    m = re.match(rf"(\w+)\s*=\s*{quoted_val}", flt)
    if m:
        return (m.group(1), "=", [m.group(2)])

    # Pattern 5: col = val (sem aspas, ex: id_flag = true)
    m = re.match(r"(\w+)\s*=\s*(\S+)", flt)
    if m:
        return (m.group(1), "=", [m.group(2)])

    return None


def _canonical_predicate(col: str, values: list[str], col_index: dict[str, dict]) -> str:
    """Gera predicado canonico para uma coluna e valores.

    - Coluna textual: TRIM(CAST(col AS VARCHAR)) = 'val' ou IN (...)
    - Coluna nao-textual: col = 'val' (ou col = val para booleanos/numeros)
    - Multi-valor textual: TRIM(CAST(col AS VARCHAR)) IN ('a', 'b')
    - Multi-valor nao-textual: col IN ('a', 'b')
    - Escapa aspas simples em valores
    """
    is_textual = _is_textual_column(col, col_index)

    # FALHA 3: Multi-valor usa IN, nao OR duplicado
    if len(values) > 1:
        escaped_vals = [_escape_sql_value(v) for v in values]
        vals_str = ", ".join(f"'{v}'" for v in escaped_vals)
        if is_textual:
            return f"TRIM(CAST({col} AS VARCHAR)) IN ({vals_str})"
        else:
            return f"{col} IN ({vals_str})"

    # Valor unico
    predicates = []
    for val in values:
        escaped_val = _escape_sql_value(val)
        # Detecta se valor e booleano ou numerico (nao precisa de aspas)
        val_lower = val.lower()
        is_literal = val_lower in ("true", "false", "null") or re.match(r"^-?\d+\.?\d*$", val)

        if is_textual:
            # Aplica TRIM(CAST(...))
            predicates.append(f"TRIM(CAST({col} AS VARCHAR)) = '{escaped_val}'")
        elif is_literal:
            # Valor literal (booleano, numero, null): sem aspas
            predicates.append(f"{col} = {val}")
        else:
            # Valor string mas coluna nao-textual (ex: DATE): com aspas
            predicates.append(f"{col} = '{escaped_val}'")

    if len(predicates) == 1:
        return predicates[0]
    return "(" + " OR ".join(predicates) + ")"


def _alias_usable(alias_norm: str) -> bool:
    """Filtra aliases perigosos: vazios, 1 char, digitos puros, stopwords PT."""
    if len(alias_norm) < 2 or alias_norm.isdigit():
        return False
    return alias_norm not in _PT_STOPWORDS


# ── TRIM global nos filtros de dimensao espacial/identificadora ──────────────
# ── TRIM contract-driven por tipo de coluna, nao por prefixo ──────────────────
# ── Predicado canonico — parseia filtro original, re-emite via _canonical_predicate ──
def _resolve_dimension_filters(p_norm: str, meta: dict) -> list[str]:
    """Resolve filtros de dimensao via value_aliases.

    - multi-valor na mesma dimensao -> `(pred1 OR pred2)` canonico
    - mesma mencao em dimensoes redundantes (id_x + nom_x) -> mantem so a de codigo
    - ignora aliases perigosos (digito puro, 1 char, stopword PT como "se")
    - TRIM(CAST(col AS VARCHAR)) para colunas textuais, cru para outras
    - Idempotente: filtros que ja vem com TRIM nao sao re-envolvidos
    """
    semantics = meta.get("semantics") or {}
    value_aliases = semantics.get("value_aliases") or {}
    col_index = _build_col_index(meta)

    # 1. Coleta (mencao_normalizada, dimensao, coluna, valor) de cada membro que casa
    # Parseamos o filtro original para extrair (col, valor) e re-emitir canonico
    matches: list[tuple[str, str, str, str]] = []  # (alias_norm, dim, col, val)
    for dim, members in value_aliases.items():
        if not isinstance(members, dict):
            continue
        for _canon, info in members.items():
            if not isinstance(info, dict):
                continue
            flt = info.get("filter")
            if not flt:
                continue
            # Parseia o filtro para extrair coluna e valor
            parsed = _parse_filter(flt)
            if not parsed:
                # Nao conseguiu parsear: usa o filtro original como fallback
                # (comportamento anterior para casos estranhos)
                for alias in info.get("aliases") or []:
                    a = _norm(str(alias))
                    if not _alias_usable(a):
                        continue
                    if _phrase_match(p_norm, str(alias)):
                        # Fallback: usa filtro original (nao ideal mas seguro)
                        matches.append((a, dim, "__RAW__", flt))
                        break
                continue

            col, _op, vals = parsed
            # Usa o primeiro valor (geralmente filtro tem 1 valor)
            val = vals[0] if vals else ""
            for alias in info.get("aliases") or []:
                a = _norm(str(alias))
                if not _alias_usable(a):
                    continue
                if _phrase_match(p_norm, str(alias)):
                    matches.append((a, dim, col, val))
                    break  # um alias por membro basta
    if not matches:
        return []

    # 2. Dedup por mencao: mesma palavra em dimensoes redundantes -> mantem a de codigo
    by_mention: dict[str, list[tuple[str, str, str]]] = {}  # alias -> [(dim, col, val)]
    for a, dim, col, val in matches:
        by_mention.setdefault(a, []).append((dim, col, val))
    chosen: list[tuple[str, str, str]] = []  # [(dim, col, val)]
    for _a, cands in by_mention.items():
        if len(cands) == 1:
            chosen.append(cands[0])
        else:
            # Prefere colunas id_ sobre outras (ex.: id_subsistema sobre nom_subsistema)
            idlike = [c for c in cands if c[1].startswith("id_") or c[1].startswith("id")]
            chosen.append((idlike or cands)[0])

    # 3. Agrupa por coluna -> multi-valor vira predicado canonico com OR
    by_col: dict[str, list[str]] = {}  # col -> [val1, val2, ...]
    raw_filters: list[str] = []  # filtros que nao puderam ser parseados
    for dim, col, val in chosen:
        if col == "__RAW__":
            # Filtro nao parseado: adiciona como esta
            if val not in raw_filters:
                raw_filters.append(val)
        else:
            vals = by_col.setdefault(col, [])
            if val not in vals:
                vals.append(val)

    # 4. Gera predicados canonicos
    result = []
    for col, vals in by_col.items():
        pred = _canonical_predicate(col, vals, col_index)
        result.append(pred)
    result.extend(raw_filters)
    return result


def _unit_of(agg_modes: dict, mode: str, metric: dict) -> str:
    entry = agg_modes.get(mode)
    if isinstance(entry, dict):
        return entry.get("unit", metric.get("unit_native", ""))
    return metric.get("unit_native", "")


# default_aggregation vem como SEMANTICO no corpus (sum/avg/max...); mapeia pro agregador SQL real
# p/ casar com a mode certa (o campo NAO e mode key em 321/333 metricas). SO agregadores SQL VERDADEIROS
# aqui — `last_or_avg`/`count_nonzero` sao POLITICAS contextuais (ex.: EAR = ULTIMO valor p/ estoque,
# nao media), NAO funcoes; resolve-las como AVG regride o EAR. Ficam de fora ->
# caem no fallback (comportamento atual preservado); tratamento correto e um item separado (ARG_MAX).
_SEMANTIC_AGG = {
    "sum": "SUM",
    "avg": "AVG",
    "average": "AVG",
    "mean": "AVG",
    "max": "MAX",
    "peak": "MAX",
    "min": "MIN",
    "count": "COUNT",
}


def detect_mode(pergunta: str, meta: dict, metric: dict) -> tuple[str, str]:
    """Detecta o modo de agregacao e retorna (modo, unidade)."""
    p_norm = _norm(pergunta)
    agg_modes = metric.get("aggregation_modes")
    if not isinstance(agg_modes, dict):
        agg_modes = {}

    for mode, triggers in _MODE_TRIGGERS:
        if mode not in agg_modes:
            continue
        if any(t.strip() in p_norm for t in triggers):
            return mode, _unit_of(agg_modes, mode, metric)

    default = metric.get("default_aggregation", "")
    default = default.strip() if isinstance(default, str) else ""  # guard: default nao-string
    if default and default in agg_modes:
        return default, _unit_of(agg_modes, default, metric)
    # default_aggregation costuma ser SEMANTICO (sum/avg/max/...), NAO uma mode key (321/333 metricas).
    # Resolve pro 1o modo cujo SQL usa esse agregador — deterministico, na ordem declarada das modes.
    # (Antes: caia no 1o modo por acidente -> 'avg' viraria SUM se total_energy viesse primeiro.)
    agg_fn = _SEMANTIC_AGG.get(default.lower()) if default else None
    if agg_fn:
        for k, v in agg_modes.items():
            raw = v.get("sql", "") if isinstance(v, dict) else v
            sql = raw.upper() if isinstance(raw, str) else ""  # guard: sql null/nao-string
            if re.search(rf"\b{agg_fn}\s*\(", sql):
                return k, _unit_of(agg_modes, k, metric)
    if agg_modes:
        first = next(iter(agg_modes))
        return first, _unit_of(agg_modes, first, metric)
    return "", ""


