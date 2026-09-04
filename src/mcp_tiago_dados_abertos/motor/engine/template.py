# -*- coding: utf-8 -*-
"""Preparacao do template: TRIM via AST, validacao do SQL e substituicao de placeholders.

Parte do motor semantico; fatiado de semantics_engine.py pelas suas proprias secoes.
"""
from __future__ import annotations

import re
from typing import Optional

import sqlglot
from sqlglot import exp

from mcp_tiago_dados_abertos.motor.pruning import prune_sql

from .ambiguidade import _explicit_mode
from .normalizacao import _norm
from .parametros import _NON_TEXTUAL_TYPES, _build_col_index, _is_scalar_literal, _parse_filter
from .patterns import _YEAR_PLACEHOLDERS, _fill_params, _resolve_placeholder_from_question, _score_pattern

# ── TRIM via AST: Substitui o pós-processamento regex global ──────────────────
# A abordagem anterior (_apply_trim_to_varchar_predicates) usava regex e quebrava
# JOINs validados. A nova abordagem usa sqlglot para editar APENAS predicados
# escalares no WHERE do SELECT principal.


def _trim_where_dimension_eqs(sql: str, col_index: dict[str, dict]) -> str:
    """Aplica TRIM em predicados EQ/IN textuais SOMENTE no WHERE do SELECT principal.

    NUNCA toca:
    - Predicados em JOIN/ON
    - Predicados em subqueries
    - Expressões/funções/colunas no lado direito

    Usa sqlglot para parse AST. Se o parse falhar, retorna o SQL original (fail-open).
    """
    if not col_index:
        return sql

    try:
        parsed = sqlglot.parse_one(sql, read="duckdb")
    except Exception:
        # Parse falhou: retorna SQL original inalterado (fail-open)
        return sql

    if parsed is None:
        return sql

    def _is_textual_by_index(col_name: str) -> bool:
        """Verifica se coluna e textual pelo col_index."""
        info = col_index.get(col_name)
        if not info:
            return False  # Coluna desconhecida: NAO aplica TRIM (conservador)
        phys_type = info.get("type", "")
        logical = info.get("logical", "")
        if phys_type and phys_type in _NON_TEXTUAL_TYPES:
            return False
        if phys_type == "VARCHAR":
            return True
        if not phys_type and logical == "string":
            return True
        return False

    def _get_column_name(node: exp.Expression) -> Optional[str]:
        """Extrai nome da coluna de um no, ignorando aliases de tabela."""
        if isinstance(node, exp.Column):
            return node.name
        return None

    def _is_string_literal(node: exp.Expression) -> bool:
        """Verifica se o no e um literal string."""
        return isinstance(node, exp.Literal) and node.is_string

    def _is_literal_list(node: exp.Expression) -> bool:
        """Verifica se o no e uma lista de literais string (para IN)."""
        if isinstance(node, exp.Tuple):
            return all(_is_string_literal(e) for e in node.expressions)
        return False

    def _wrap_column_with_trim(col_node: exp.Column) -> exp.Expression:
        """Envolve uma coluna em TRIM(CAST(col AS VARCHAR))."""
        col_name = col_node.name
        table = col_node.table
        if table:
            col_ref = f"{table}.{col_name}"
        else:
            col_ref = col_name
        # Constroi TRIM(CAST(col AS VARCHAR))
        trim_expr = sqlglot.parse_one(f"TRIM(CAST({col_ref} AS VARCHAR))", read="duckdb")
        return trim_expr

    def _is_in_where_clause(node: exp.Expression, root: exp.Expression) -> bool:
        """Verifica se o no esta dentro de um WHERE (e nao em JOIN/ON/subquery)."""
        parent = node.parent
        while parent is not None:
            # Se encontrar um JOIN antes de WHERE, nao esta no WHERE
            if isinstance(parent, exp.Join):
                return False
            # Se encontrar um ON, nao esta no WHERE
            if parent.key == "on":
                return False
            # Se encontrar uma subquery, nao esta no WHERE principal
            if isinstance(parent, exp.Subquery):
                return False
            # Se encontrar um CTE (With), para — estamos analisando o CTE, nao o SELECT principal
            # Mas queremos que o WHERE do CTE TAMBEM seja processado? Sim, para simplicidade.
            if isinstance(parent, exp.Where):
                return True
            parent = parent.parent
        return False

    # Coleta predicados EQ e IN no WHERE que precisam de TRIM
    changes_made = False

    for node in list(parsed.walk()):
        if not _is_in_where_clause(node, parsed):
            continue

        # Processa EQ: col = 'val'
        if isinstance(node, exp.EQ):
            left = node.left
            right = node.right
            col_name = _get_column_name(left)
            if col_name and _is_textual_by_index(col_name) and _is_string_literal(right):
                # Substitui left por TRIM(CAST(...))
                new_left = _wrap_column_with_trim(left)
                node.set("this", new_left)
                changes_made = True

        # Processa IN: col IN ('a', 'b')
        elif isinstance(node, exp.In):
            left = node.this
            right = node.expressions if hasattr(node, "expressions") else None
            # Para exp.In, os valores estao em node.expressions ou node.query
            col_name = _get_column_name(left)
            if col_name and _is_textual_by_index(col_name):
                # Verifica se todos os elementos sao literais string
                in_values = node.expressions if node.expressions else []
                if in_values and all(_is_string_literal(v) for v in in_values):
                    new_left = _wrap_column_with_trim(left)
                    node.set("this", new_left)
                    changes_made = True

    if changes_made:
        return parsed.sql(dialect="duckdb")
    return sql


def _is_valid_sql_statement(sql: str) -> bool:
    """Verifica se o SQL é um statement completo (SELECT ou WITH).

    Templates-fragmento (ex: WHERE ...) devem ser rejeitados para evitar SQL malformado.

    Retorna True se o SQL parseia como SELECT ou WITH válido.
    """
    try:
        parsed = sqlglot.parse_one(sql, read="duckdb")
        if parsed is None:
            return False
        # Aceita SELECT ou WITH (que contem SELECT)
        return isinstance(parsed, (exp.Select, exp.With))
    except Exception:
        return False


def _prepare_template_for_validation(template: str, source: str) -> str:
    """Prepara template para validacao, substituindo placeholders por valores dummy.

    Trata placeholders com e sem aspas:
    - {col} -> __TEST__
    - '{col}' -> '__TEST__' (remove aspas externas, adiciona aspas no valor)
    """
    # Substitui {source} primeiro
    result = re.sub(r"\{source\}", source, template)
    # Substitui '{placeholder}' (com aspas) por '__TEST__'
    result = re.sub(r"'\{[a-z_]+\}'", "'__TEST__'", result)
    # Substitui {placeholder} (sem aspas) por __TEST__
    result = re.sub(r"\{[a-z_]+\}", "__TEST__", result)
    return result


def _deve_ancorar_recencia(p_norm: str, params: dict) -> bool:
    """Gate da ancora A2: recencia ESTRITA (fronteira lexical; exclui 'penultimo')
    e NENHUM ano/intervalo ja resolvido em params (o extrator ja separou ano-real
    de quantidade, ex.: '2000 MW'). Chave temporal None nao bloqueia (== ausencia)."""
    if re.search(r"pen[uú]ltim", p_norm):
        return False
    if not re.search(r"\b(ultim[oa]s?|mais recente|atual|vigente|hoje)\b", p_norm):
        return False
    blocked = set(_YEAR_PLACEHOLDERS) | {
        "year_start", "year_end", "data_inicio", "data_fim", "start_date", "end_date"}
    return not any(params.get(k) is not None for k in blocked)


def _anchor_year_to_latest(template: str) -> str:
    """Recencia sem ano literal: ancora `<expr> = {year}` no maior ano presente
    no dado via subquery MAX, em vez do ano corrente (que da vazio quando o dado
    termina antes do calendario). Espelha a MESMA expressao dos dois lados;
    {source} e preenchido depois por _fill_params. NAO ancora quando: (a) expr tem
    alias qualificado (`t.col`) — ficaria unbound na subquery; (b) o match esta
    dentro de string literal."""
    _EXPR = r"(?P<expr>[\w.]+(?:\s*\((?:[^()]|\([^()]*\))*\))?)\s*=\s*\{"
    for ph in _YEAR_PLACEHOLDERS:
        subject = template

        def _repl(m: "re.Match", _s: str = subject) -> str:
            expr = m.group("expr")
            if "." in expr:  # alias qualificado -> unbound na subquery
                return m.group(0)
            if _s[: m.start()].count("'") % 2 == 1:  # dentro de string literal
                return m.group(0)
            return f"{expr} = (SELECT MAX({expr}) FROM {{source}})"

        template = re.sub(_EXPR + re.escape(ph) + r"\}", _repl, template)
    return template


def render_sql_pattern(pergunta: str, meta: dict, params: dict, mode: str) -> Optional[tuple[str, str]]:
    """Seleciona e renderiza o melhor sql_pattern. Retorna (sql, pattern_name) ou None.

    Rejeita templates que não são SELECT/WITH completos para evitar SQL malformado.
    """
    semantics = meta.get("semantics") or {}
    patterns = semantics.get("sql_patterns") or {}
    if not isinstance(patterns, dict) or not patterns:
        return None

    source = meta.get("parquet_source") or meta.get("s3_location") or ""
    if not source:
        return None

    # Pattern explicitamente `validated: false` (validation_error/0-rows) ranqueia
    # ABAIXO de qualquer outro (validado OU sem o campo): senao um pattern especial
    # nao-provado (ex.: bombeamento WHERE val_geracao<0, unico com output_modes=
    # [time_series]) sequestra perguntas genericas "por mes"/"serie" via o +5 de mode.
    # Chave primaria = validado-nao-falso; secundaria = score. Nao-validado continua
    # selecionavel como FALLBACK quando nao ha alternativa.
    # Se a pergunta pede um modo EXPLICITO (pico, mwmed, gwh/energia), DESQUALIFICA
    # patterns cujo output_modes declarado NAO inclui esse modo — senao um pattern
    # total_energy (GWh) vence "pico"/"mwmed" so pelo keyword do nome (colisao real,
    # ). Sem candidato compativel, cai no metric-path (que trata pico via
    # MAX, average_power via SIN/rollup). Vale so p/ modo explicito: query generica nao filtra.
    # Filtro ESTREITO só p/ PICO: pergunta de peak_power explicito nunca deve pegar um
    # pattern total_energy (GWh) — pico somado como energia (SUM*24) e sempre errado.
    # Sem candidato, cai no metric-path (MAX). NAO toca average_power/time_series (evita
    # a regressao do "taxa media": 'average' != 'average_power', e mwmed ja resolve via +5).
    _peak_q = _explicit_mode(pergunta) == "peak_power"

    def _peak_ok(body: dict) -> bool:
        if not _peak_q:
            return True
        # pergunta de PICO: só sobrevive pattern que declara peak_power; senao cai no
        # metric-path (MAX). Vale SO p/ peak_power — average_power/time_series intactos.
        oms = (body.get("matches") or {}).get("output_modes") or []
        return not (oms and "peak_power" not in oms)

    ranked = sorted(
        (
            (name, body)
            for name, body in patterns.items()
            if isinstance(body, dict) and body.get("template") and _peak_ok(body)
        ),
        key=lambda kv: _score_pattern(kv[0], kv[1], pergunta, mode, params),
        reverse=True,
    )

    p_norm = _norm(pergunta)
    # Placeholders genericos ja mapeados pelo engine (nao sao dimensoes)
    _generic_keys = {
        "source",
        "year",
        "year_filter",
        "year_start",
        "year_end",
        "month",
        "n",
        "top_n",
        "data_inicio",
        "data_fim",
        "start_date",
        "end_date",
        "start",
        "end",
        "data",
    }

    for name, body in ranked:
        template = body["template"]

        # Valida se o template é um SELECT/WITH completo ANTES de tentar preencher.
        # Fragmentos são rejeitados. Usa uma versão preenchida com placeholders
        # dummy para validar estrutura.
        template_test = _prepare_template_for_validation(template, source)
        if not _is_valid_sql_statement(template_test):
            # Template é fragmento: pula para o próximo pattern
            continue

        # A2+ (recencia): pergunta pede o mais recente e NAO traz ano literal nem
        # ano em params -> ancora {year} no maior ano do dado (subquery MAX),
        # robusto a data-lag. Sem sinal de recencia OU com ano literal, segue o
        # caminho normal (ano corrente).
        if _deve_ancorar_recencia(p_norm, params):
            template = _anchor_year_to_latest(template)

        # Resolve placeholders de dimensão a partir da pergunta ANTES de cair
        # no validated_params. Enriquece params com valores extraídos.
        enriched_params = dict(params)
        placeholders_in_template = set(re.findall(r"\{([a-z_]+)\}", template))
        for ph in placeholders_in_template - _generic_keys:
            if enriched_params.get(ph) is not None:
                continue  # já resolvido pelo caller (None explícito NÃO bloqueia)
            resolved = _resolve_placeholder_from_question(ph, p_norm, meta)
            if resolved is not None:
                enriched_params[ph] = resolved

        # Se dimension_filters tem filtro para uma coluna que corresponde a um
        # placeholder não resolvido, extrai o valor do filtro para usar no template.
        # Isso corrige casos onde _resolve_placeholder_from_question falha para
        # aliases curtos (ex: 'sul' = 3 chars) mas _resolve_dimension_filters funciona.
        dimension_filters = enriched_params.get("dimension_filters") or []
        for flt in dimension_filters:
            parsed = _parse_filter(flt)
            if not parsed:
                continue
            col, _op, vals = parsed
            if not vals:
                continue
            val = vals[0]
            # Verifica se algum placeholder nao resolvido corresponde a essa coluna
            for ph in placeholders_in_template - _generic_keys:
                if enriched_params.get(ph) is not None:
                    continue
                # Heuristica: placeholder casa com coluna se um contem o outro
                ph_norm = _norm(ph)
                col_norm = _norm(col)
                if ph_norm in col_norm or col_norm.endswith(ph_norm):
                    enriched_params[ph] = val
                    break

        sql = _fill_params(
            template,
            source,
            enriched_params,
            validated_params=body.get("validated_params"),
        )
        if not sql:
            continue

        # Valida SQL final ANTES de retornar
        # Se nao for SELECT/WITH valido, pula para o proximo pattern
        if not _is_valid_sql_statement(sql):
            continue

        # C5: injeta filtros de dimensao ANTES de aplicar TRIM
        # (para que o filtro correto seja usado, nao o validated_params default)
        sql = _inject_dimension_filters(sql, enriched_params)
        # Aplica TRIM via AST (sqlglot), so no WHERE, sem tocar JOIN/ON
        col_index = _build_col_index(meta)
        sql = _trim_where_dimension_eqs(sql, col_index)
        # pattern-path: poda o glob SO se seguro (1 read_parquet, sem subquery MAX) — ver prune_sql
        # Poda por ano SO com janela de ano UNICO: em intervalo multi-ano
        # (year_start != year_end) podar pelo primeiro ano truncaria
        # silenciosamente a parte recente do intervalo.
        prune_year = params.get("year")
        _ys, _ye = params.get("year_start"), params.get("year_end")
        if _ys is not None and _ye is not None and _ys != _ye:
            prune_year = None
        sql = prune_sql(sql.strip(), meta.get("prune_partition"), prune_year)
        return sql, name
    return None


def _mask_string_literals(sql: str) -> tuple[str, dict[str, str]]:
    """Mascara string literals no SQL para evitar edicao dentro de aspas.

    Retorna (sql_mascarado, mapa_de_restauracao).
    """
    placeholder_map: dict[str, str] = {}
    counter = [0]

    def replacer(match: re.Match) -> str:
        token = f"__LITERAL_{counter[0]}__"
        counter[0] += 1
        placeholder_map[token] = match.group(0)
        return token

    # Mascara strings entre aspas simples (incluindo escaped '')
    masked = re.sub(r"'(?:[^']|'')*'", replacer, sql)
    return masked, placeholder_map


def _extract_predicate_column(predicate: str) -> Optional[str]:
    """Extrai o nome da coluna de um predicado SQL.

    Suporta:
    - col = 'val'
    - col = val
    - TRIM(col) = 'val'
    - TRIM(CAST(col AS VARCHAR)) = 'val'
    """
    # TRIM(CAST(col AS VARCHAR)) = ...
    m = re.match(r"TRIM\s*\(\s*CAST\s*\(\s*(\w+)\s+AS", predicate, re.IGNORECASE)
    if m:
        return m.group(1)

    # TRIM(col) = ...
    m = re.match(r"TRIM\s*\(\s*(\w+)\s*\)", predicate, re.IGNORECASE)
    if m:
        return m.group(1)

    # col = ... ou col IN (...)
    m = re.match(r"(\w+)\s*(?:=|IN\s*\()", predicate, re.IGNORECASE)
    if m:
        return m.group(1)

    return None


def _sql_has_column_predicate(sql_masked: str, col: str) -> bool:
    """Verifica se o SQL mascarado ja tem um predicado sobre a coluna.

    Detecta padroes como:
    - col = ...
    - t.col = ... (com alias)
    - TRIM(col) = ...
    - TRIM(CAST(col AS VARCHAR)) = ...
    """
    col_esc = re.escape(col)
    patterns = [
        rf"\bTRIM\s*\(\s*CAST\s*\(\s*{col_esc}\s+AS\s+VARCHAR",
        rf"\bTRIM\s*\(\s*{col_esc}\s*\)",
        rf"(?:\b\w+\.)?{col_esc}\s*=",
    ]
    for pat in patterns:
        if re.search(pat, sql_masked, re.IGNORECASE):
            return True
    return False


def _inject_dimension_filters(sql: str, params: dict) -> str:
    """Injeta filtros de value_aliases, EVITANDO duplicacao de predicados.

    Usa AST via sqlglot para verificar predicados SOMENTE no WHERE
    do SELECT principal. Predicados em JOIN/ON/CTE/subquery NAO contam como duplicata.

    Estrategia:
    - Se o SQL JA tem predicado na coluna NO WHERE PRINCIPAL: SUBSTITUI pelo canonico
    - Se o SQL NAO tem predicado na coluna no WHERE: ADICIONA o canonico
    - Predicados em JOIN/ON NAO contam — o filtro e adicionado ao WHERE
    """
    filters = params.get("dimension_filters") or []
    if not filters:
        return sql

    # Tenta usar AST para injecao precisa
    try:
        return _apply_dimension_filters_ast(sql, filters)
    except Exception:
        # Fallback: usa a logica regex conservadora (comportamento anterior)
        return _inject_dimension_filters_regex(sql, filters)


def _get_main_table_alias(main_select: exp.Select) -> Optional[str]:
    """Extrai o alias da relacao read_parquet (a FONTE de dados) do SELECT.

    Em vez de pegar o alias da primeira relacao do FROM, procura
    especificamente a relacao cujo expr e read_parquet(...) — a fonte de dados.
    So qualifica com o alias DELA. Se nao achar/provar -> retorna None -> OMITE.

    Usa args.get("from_") em vez de find(exp.From) para pegar apenas o FROM
    direto do main_select, nao de CTEs/subqueries.
    NOTA: sqlglot usa "from_" (com underscore) em vez de "from"

    Retorna o alias se existir (ex: 't' em 'FROM read_parquet(...) AS t'), ou None.
    """
    # args.get em vez de find
    from_clause = main_select.args.get("from_") or main_select.args.get("from")
    if not from_clause:
        return None

    # Funcao auxiliar para verificar se um node contem read_parquet
    def _is_read_parquet_expr(node: exp.Expression) -> bool:
        """Verifica se o node e ou contem uma chamada read_parquet(...)."""
        # Verifica se e ReadParquet diretamente
        # sqlglot parseia read_parquet como exp.ReadParquet (nao Anonymous)
        try:
            if type(node).__name__ == "ReadParquet":
                return True
        except Exception:
            pass
        # Verifica Anonymous como fallback
        if isinstance(node, exp.Anonymous) and node.name and node.name.lower() == "read_parquet":
            return True
        # Verifica se o node tem um filho que e ReadParquet
        if hasattr(node, "this"):
            inner = node.this
            if inner is not None:
                try:
                    if type(inner).__name__ == "ReadParquet":
                        return True
                except Exception:
                    pass
                if isinstance(inner, exp.Anonymous) and inner.name and inner.name.lower() == "read_parquet":
                    return True
        # Verifica nos filhos via walk (limitado a 2 niveis)
        try:
            for child in node.iter_expressions():
                try:
                    if type(child).__name__ == "ReadParquet":
                        return True
                except Exception:
                    pass
                if isinstance(child, exp.Anonymous) and child.name and child.name.lower() == "read_parquet":
                    return True
        except Exception:
            pass
        return False

    # Funcao auxiliar para extrair alias de um table_expr
    def _extract_alias(table_expr: exp.Expression) -> Optional[str]:
        # Para Table com alias direto
        if hasattr(table_expr, "alias") and table_expr.alias:
            return table_expr.alias
        # Para Alias wrapper
        if isinstance(table_expr, exp.Alias):
            return table_expr.alias
        return None

    # Verifica a tabela principal do FROM
    table_expr = from_clause.this
    if table_expr is not None:
        # Verifica se a tabela principal contem read_parquet
        if _is_read_parquet_expr(table_expr):
            alias = _extract_alias(table_expr)
            if alias:
                return alias

    # NAO procura em JOINs quando o FROM principal NAO e read_parquet.
    # Se o FROM principal e um CTE/subquery/tabela, qualificar com alias de JOIN
    # causa Binder Error (a coluna pode nao existir no JOIN).
    # A busca em JOINs so faz sentido se o FROM.this for read_parquet COM alias
    # (nao e o caso quando table_expr nao e read_parquet).
    #
    # O comportamento correto e: se o FROM.this NAO e read_parquet direto,
    # retorna None e o filtro sera OMITIDO (fail-safe).

    # Se nao encontrou read_parquet especificamente, retorna None
    # (fail-safe: nao qualifica quando nao pode provar qual e a fonte)
    return None


def _select_has_joins(main_select: exp.Select) -> bool:
    """Verifica se o SELECT principal tem JOINs (apenas no nivel do SELECT, nao em subqueries).

    Usa main_select.args.get("joins") para verificar JOINs apenas no
    SELECT principal, NAO em subqueries descendentes (evita falsos positivos).
    """
    joins = main_select.args.get("joins")
    return bool(joins)


def _qualify_predicate_column(predicate: str, table_alias: str) -> str:
    """Qualifica a coluna do predicado com o alias da tabela.

    Transforma:
    - TRIM(CAST(col AS VARCHAR)) = 'val' -> TRIM(CAST(t.col AS VARCHAR)) = 'val'
    - col = 'val' -> t.col = 'val'
    - col IN ('a', 'b') -> t.col IN ('a', 'b')

    Se o predicado JA tem alias (ex: t.col), preserva-o.

    Fail-safe: se nao conseguir qualificar com seguranca, retorna None
    para indicar que o filtro NAO deve ser injetado (evita Binder Error).
    NAO retorna o predicado cru quando nao pode qualificar.
    """
    if not table_alias:
        # Sem alias, retorna None (OMITE o filtro)
        return None

    # Se ja tem alias (ex: t.col ou v.col), preserva
    # Detecta: alias.col = ou CAST(alias.col ou TRIM(alias.col
    if re.search(r"\b\w+\.\w+\s*(?:=|IN\s*\()", predicate, re.IGNORECASE):
        return predicate
    if re.search(r"CAST\s*\(\s*\w+\.\w+", predicate, re.IGNORECASE):
        return predicate
    if re.search(r"TRIM\s*\(\s*\w+\.\w+", predicate, re.IGNORECASE):
        return predicate

    # Qualifica a coluna: TRIM(CAST(col AS -> TRIM(CAST(t.col AS
    # Pattern: TRIM(CAST(col AS VARCHAR))
    pattern_trim_cast = r"TRIM\s*\(\s*CAST\s*\(\s*(\w+)\s+AS"
    m = re.search(pattern_trim_cast, predicate, re.IGNORECASE)
    if m:
        col = m.group(1)
        # Substitui col por alias.col
        return re.sub(
            pattern_trim_cast,
            f"TRIM(CAST({table_alias}.{col} AS",
            predicate,
            count=1,
            flags=re.IGNORECASE,
        )

    # Pattern: TRIM(col) = ...
    pattern_trim = r"TRIM\s*\(\s*(\w+)\s*\)"
    m = re.search(pattern_trim, predicate, re.IGNORECASE)
    if m:
        col = m.group(1)
        return re.sub(
            pattern_trim,
            f"TRIM({table_alias}.{col})",
            predicate,
            count=1,
            flags=re.IGNORECASE,
        )

    # Pattern: col = val ou col IN (...)
    pattern_col = r"^(\w+)\s*(=|IN\s*\()"
    m = re.match(pattern_col, predicate, re.IGNORECASE)
    if m:
        col = m.group(1)
        m.group(2)
        # rest comeca APOS a coluna + espacos, entao inclui o operador
        rest = predicate[m.start(2) :]
        return f"{table_alias}.{col} {rest}"

    # Nao conseguiu qualificar: retorna None (OMITE o filtro)
    # Omitir e seguro: os patterns ja trazem sua propria logica, e o analisar
    # tem fallback deterministico. Melhor perder um filtro raro do que Binder Error.
    return None


def _apply_dimension_filters_ast(sql: str, filters: list[str]) -> str:
    """Aplica filtros de dimensao via AST, escopado ao WHERE do SELECT principal.

    Principio "prova-que-e-seguro-ou-OMITE"

    A injecao de um dimension_filter no WHERE so acontece se TODAS as condicoes forem provadas:
    - (a) Filtro ATOMICO escalar: exatamente col = 'literal' ou col IN ('a','b',...).
          Qualquer coisa composta (window OVER, subquery, funcao, IS NULL, AND/OR) -> OMITE.
    - (b) Coluna atribuivel a FONTE de dados: a coluna e da relacao read_parquet(...).
          Qualifica com o alias DELA quando ha JOIN.
    - (c) Se nao der pra provar (a)+(b) -> OMITE o filtro (nao injeta nada).

    Omitir e seguro: os patterns ja trazem sua propria logica (ex.: taxa_mensal_versao_maxima
    ja faz versao-maxima via CTE), e o analisar tem fallback deterministico.
    Melhor perder um filtro raro do que gerar Binder/Parser Error.

    Raises:
        Exception: se o parse falhar (caller deve fazer fallback)
    """
    parsed = sqlglot.parse_one(sql, read="duckdb")
    if parsed is None:
        raise ValueError("Parse retornou None")

    # Localiza o SELECT principal (mais externo, ignorando CTEs)
    main_select = _find_main_select(parsed)
    if main_select is None:
        raise ValueError("Nao encontrou SELECT principal")

    # Detecta JOINs e extrai alias da fonte (read_parquet)
    has_joins = _select_has_joins(main_select)
    main_table_alias = _get_main_table_alias(main_select)

    # Usa args.get("from_") em vez de find(exp.From)
    # find() faz busca descendente e pode achar FROM de CTEs/subqueries.
    # NOTA: sqlglot usa "from_" (com underscore) em vez de "from"
    from_clause = main_select.args.get("from_") or main_select.args.get("from")
    source_is_read_parquet = False
    if from_clause and from_clause.this:
        table_expr = from_clause.this
        # Verifica se o FROM.this E read_parquet (nao em subquery)
        # Table com ReadParquet como .this
        if hasattr(table_expr, "this"):
            inner = table_expr.this
            try:
                if type(inner).__name__ == "ReadParquet":
                    source_is_read_parquet = True
            except Exception:
                pass
        # Ou e ReadParquet diretamente
        try:
            if type(table_expr).__name__ == "ReadParquet":
                source_is_read_parquet = True
        except Exception:
            pass
        # Verifica se e Alias de ReadParquet
        if isinstance(table_expr, exp.Alias) and hasattr(table_expr, "this"):
            inner = table_expr.this
            try:
                if type(inner).__name__ == "ReadParquet":
                    source_is_read_parquet = True
            except Exception:
                pass
            # Ou Table contendo ReadParquet
            if hasattr(inner, "this"):
                inner2 = inner.this
                try:
                    if type(inner2).__name__ == "ReadParquet":
                        source_is_read_parquet = True
                except Exception:
                    pass

    # Extrai colunas dos filtros a injetar
    filter_by_col: dict[str, str] = {}  # col_lower -> filtro canonico (ja qualificado)
    for flt in filters:
        # Verifica se o filtro e atomico escalar ANTES de tentar injetar
        # Filtros complexos (MAX OVER, subquery, IS NULL, AND/OR) sao OMITIDOS
        if not _is_scalar_literal(flt):
            # Filtro complexo: OMITE (nao injeta)
            continue

        col = _extract_predicate_column(flt)
        if col:
            # Qualifica o filtro se o SELECT tem JOINs
            qualified_flt = flt
            if has_joins:
                if main_table_alias:
                    qualified_flt = _qualify_predicate_column(flt, main_table_alias)
                    # FAIL-SAFE: se a qualificacao falhou (retornou None), OMITE
                    if qualified_flt is None:
                        continue
                else:
                    # JOIN sem alias da fonte -> OMITE o filtro
                    continue
            elif not source_is_read_parquet:
                # Fonte nao e read_parquet direto (ex: subquery) -> OMITE
                continue
            filter_by_col[col.lower()] = qualified_flt
        else:
            # Filtro nao parseavel (coluna nao extraida) -> OMITE por seguranca
            continue

    # Usa main_select.args.get("where") em vez de find(exp.Where)
    # find() faz busca descendente e pode achar WHERE de CTEs/subqueries.
    # args.get() pega apenas o WHERE direto do main_select.
    where_clause = main_select.args.get("where")
    existing_cols_in_where: set[str] = set()
    if where_clause:
        existing_cols_in_where = _extract_predicate_columns_from_where(where_clause)

    # Determina quais filtros adicionar vs substituir
    to_add: list[str] = []
    to_replace: dict[str, str] = {}  # col_lower -> novo predicado (qualificado)

    for col_lower, flt in filter_by_col.items():
        # Nao ha mais __raw_ — todos os filtros foram parseados ou omitidos
        if col_lower in existing_cols_in_where:
            # Coluna ja tem predicado no WHERE: marca para substituir
            to_replace[col_lower] = flt
        else:
            # Coluna nao tem predicado no WHERE: adiciona
            to_add.append(flt)

    # Aplica substituicoes no WHERE existente
    # As substituicoes tambem precisam ser qualificadas
    if to_replace and where_clause:
        _replace_predicates_in_where(where_clause, to_replace)

    # Adiciona novos predicados ao WHERE
    if to_add:
        new_conditions = " AND ".join(to_add)
        new_cond_expr = sqlglot.parse_one(new_conditions, read="duckdb")
        if where_clause:
            # WHERE existe: adiciona com AND
            old_cond = where_clause.this
            combined = exp.And(this=old_cond, expression=new_cond_expr)
            where_clause.set("this", combined)
        else:
            # Cria WHERE no main_select, nao via busca descendente
            new_where = exp.Where(this=new_cond_expr)
            main_select.set("where", new_where)

    return parsed.sql(dialect="duckdb")


def _find_main_select(parsed: exp.Expression) -> Optional[exp.Select]:
    """Encontra o SELECT principal (mais externo, ignorando CTEs).

    Em uma query WITH ... SELECT, retorna o SELECT final.
    """
    if isinstance(parsed, exp.Select):
        return parsed

    # Para WITH, o SELECT principal esta em `.this`
    if isinstance(parsed, exp.With):
        inner = parsed.this
        if isinstance(inner, exp.Select):
            return inner

    # Busca recursiva como fallback
    for node in parsed.walk():
        if isinstance(node, exp.Select):
            # Ignora subqueries (tem parent que nao e With)
            parent = node.parent
            if parent is None or isinstance(parent, exp.With):
                return node

    return None


def _extract_predicate_columns_from_where(where_clause: exp.Where, only_positive: bool = True) -> set[str]:
    """Extrai nomes de colunas usadas em predicados no WHERE.

    Quando only_positive=True (padrao), ignora predicados NEGATIVOS
    (NOT IN, NOT col = ..., col != ...) — esses NAO bloqueiam a injecao de um filtro
    POSITIVO adicional (o filtro positivo sera adicionado com AND).

    Retorna set de nomes em lowercase.
    """
    cols: set[str] = set()
    if not where_clause or not where_clause.this:
        return cols

    # Coleta nos que estao sob NOT para excluir do resultado positivo
    nodes_under_not: set[int] = set()
    if only_positive:
        for node in where_clause.this.walk():
            if isinstance(node, exp.Not):
                # Marca todos os descendentes deste NOT
                for child in node.walk():
                    nodes_under_not.add(id(child))
            # NEQ (!=) tambem e predicado negativo
            if isinstance(node, exp.NEQ):
                nodes_under_not.add(id(node))

    def _extract_col_from_left(left: exp.Expression) -> Optional[str]:
        """Extrai nome da coluna do lado esquerdo de EQ/IN."""
        if isinstance(left, exp.Column):
            return left.name.lower()
        elif isinstance(left, exp.Trim):
            inner = left.this
            if isinstance(inner, exp.Column):
                return inner.name.lower()
            elif isinstance(inner, exp.Cast) and isinstance(inner.this, exp.Column):
                return inner.this.name.lower()
        return None

    for node in where_clause.this.walk():
        # Se only_positive e este no esta sob NOT, ignora
        if only_positive and id(node) in nodes_under_not:
            continue

        # EQ: col = val
        if isinstance(node, exp.EQ):
            col = _extract_col_from_left(node.this)
            if col:
                cols.add(col)
        # IN: col IN (...) — so conta como positivo se NAO estiver sob NOT
        elif isinstance(node, exp.In):
            col = _extract_col_from_left(node.this)
            if col:
                cols.add(col)

    return cols


def _replace_predicates_in_where(where_clause: exp.Where, replacements: dict[str, str]) -> None:
    """Substitui predicados existentes no WHERE por novos.

    Modifica a AST in-place.

    Args:
        where_clause: clausula WHERE
        replacements: dict col_lower -> novo predicado SQL string
    """
    if not where_clause or not where_clause.this:
        return

    def _get_col_name(node: exp.Expression) -> Optional[str]:
        """Extrai nome da coluna de um no EQ ou IN."""
        if isinstance(node, (exp.EQ, exp.In)):
            left = node.this
            if isinstance(left, exp.Column):
                return left.name.lower()
            elif isinstance(left, exp.Trim):
                inner = left.this
                if isinstance(inner, exp.Column):
                    return inner.name.lower()
                elif isinstance(inner, exp.Cast) and isinstance(inner.this, exp.Column):
                    return inner.this.name.lower()
        return None

    def _replace_in_tree(node: exp.Expression) -> exp.Expression:
        """Percorre a arvore e substitui predicados."""
        col = _get_col_name(node)
        if col and col in replacements:
            new_sql = replacements[col]
            new_node = sqlglot.parse_one(new_sql, read="duckdb")
            return new_node

        # Recursao para AND/OR
        if isinstance(node, exp.And):
            node.set("this", _replace_in_tree(node.this))
            node.set("expression", _replace_in_tree(node.expression))
        elif isinstance(node, exp.Or):
            node.set("this", _replace_in_tree(node.this))
            node.set("expression", _replace_in_tree(node.expression))
        elif isinstance(node, exp.Paren):
            node.set("this", _replace_in_tree(node.this))

        return node

    new_condition = _replace_in_tree(where_clause.this)
    where_clause.set("this", new_condition)


def _inject_dimension_filters_regex(sql: str, filters: list[str]) -> str:
    """Fallback: injeta filtros usando regex (comportamento anterior).

    Usado quando o parse AST falha.
    """
    # 1. Mascara string literals
    sql_masked, literal_map = _mask_string_literals(sql)

    # 2. Para cada filtro, verifica se a coluna ja tem predicado no SQL
    # Se nao tem, marca para injetar
    additions = []
    for flt in filters:
        col = _extract_predicate_column(flt)
        if not col:
            # Filtro nao parseavel: injeta como esta
            additions.append(flt)
            continue
        if _sql_has_column_predicate(sql_masked, col):
            # SQL ja tem predicado nessa coluna: NAO injetar (evita duplicacao)
            continue
        additions.append(flt)

    if not additions:
        return sql

    clause = " AND ".join(additions)
    if re.search(r"\bWHERE\b", sql, re.IGNORECASE):
        return re.sub(r"\bWHERE\b", f"WHERE {clause} AND", sql, count=1, flags=re.IGNORECASE)
    # Nao tem WHERE: insere antes de GROUP BY / ORDER BY / QUALIFY / LIMIT
    for kw in ["GROUP BY", "ORDER BY", "QUALIFY", "LIMIT"]:
        if re.search(rf"\b{kw}\b", sql, re.IGNORECASE):
            return re.sub(rf"\b{kw}\b", f"WHERE {clause} {kw}", sql, count=1, flags=re.IGNORECASE)
    return f"{sql} WHERE {clause}"


