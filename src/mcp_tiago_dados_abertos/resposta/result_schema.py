# -*- coding: utf-8 -*-
"""result_schema — tipagem do RESULTADO de um SQL pelo CONTRATO + AST.

Para cada coluna do resultado: papel, agregador, colunas-fonte, metrica do
contrato, quantity_kind, UNIDADE com o tier que a provou e ADITIVIDADE (decidida
separadamente da unidade — SUM(col) nao prova que somar e legitimo).

Tiers de unidade (parecer, Q3 — ordem de confianca):
  pattern_output_unit  rota exata de sql_pattern -> output_unit (SO quando o resultado tem
                       exatamente 1 medida nao-COUNT e a unidade e simples)
  mode_ast             expressao AST-equivalente a um aggregation_mode -> mode.unit
  column_native        coluna da metrica (direta, agregada ou herdada por CTE) -> unit_native
  ast_share            100 * (agregado / agregado) -> '%'
  ast_pct              100 * (expr / expr) -> '%' (percentual provado pela ESTRUTURA)
  count                COUNT -> sem unidade fisica
  name_hint            so pelo nome (pct/taxa/...) — o tier mais fraco, declarado
  unknown              nada provou

LINEAGE RELACIONAL: cada alias do
FROM/JOIN e uma ORIGEM — leitor (read_parquet/read_csv) -> contrato, ou CTE/subquery ->
projecoes internas com o proprio escopo. A tipagem desce recursivamente pela expressao:
coluna -> origem -> (metrica do contrato | expressao interna da CTE); cast DECLARADO no
contrato = a propria coluna; casts numericos/ROUND/COALESCE/parenteses/alias/*1 sao
transparentes; SUM/AVG/MAX/MIN herdam a unidade do argumento; soma/subtracao so de
unidades IGUAIS; fator literal (/1000, *24) torna a unidade desconhecida (aditividade
fica); razao de duas grandezas e adimensional e, vezes 100, e percentual. Origem
ambigua (coluna sem qualificador que 2+ contratos declaram) ou desconhecida => unknown.
Nunca lanca.
"""
from __future__ import annotations

import hashlib
import re
from decimal import Decimal, InvalidOperation

import sqlglot
from sqlglot import exp

from mcp_tiago_dados_abertos.resposta.computed_facts import _RELATIVA_COL_RE, _TEMPORAL_COL_RE, _coluna_iso, _sem_acento

_RATIO_KINDS = frozenset({"percent", "percentage", "ratio", "share", "proportion"})
_NUMERIC_TYPES = frozenset({
    exp.DataType.Type.DOUBLE, exp.DataType.Type.FLOAT, exp.DataType.Type.DECIMAL,
    exp.DataType.Type.INT, exp.DataType.Type.BIGINT, exp.DataType.Type.SMALLINT,
    exp.DataType.Type.TINYINT, exp.DataType.Type.UBIGINT, exp.DataType.Type.UINT,
})
_WS_RE = re.compile(r"\s+")
_LEITORES = ("read_parquet", "read_csv", "read_csv_auto", "read_json", "read_json_auto")
_MAX_PROFUNDIDADE = 12


# ── AST ──────────────────────────────────────────────────────────────────────
def _is_num(node, valor) -> bool:
    if not isinstance(node, exp.Literal) or node.is_string:
        return False
    try:
        return Decimal(str(node.this)) == valor
    except InvalidOperation:
        return False


def _norm_node(node):
    if isinstance(node, exp.Paren):
        return node.this
    if isinstance(node, exp.Alias):
        return node.this
    if isinstance(node, (exp.Cast, exp.TryCast)) and isinstance(node.to, exp.DataType):
        if node.to.this in _NUMERIC_TYPES:
            return node.this
    if isinstance(node, exp.Mul):
        if _is_num(node.expression, 1):
            return node.this
        if _is_num(node.this, 1):
            return node.expression
    if isinstance(node, exp.Column):
        return exp.Column(this=exp.to_identifier(node.name.lower()))
    if isinstance(node, exp.Literal) and not node.is_string:
        try:
            d = Decimal(str(node.this)).normalize()
            return exp.Literal.number(format(d, "f"))
        except InvalidOperation:
            return node
    return node


def _canonico(node) -> str:
    """Forma canonica (string) de uma expressao para comparacao AST-equivalente."""
    t = node.copy()
    for _ in range(6):
        novo = t.transform(_norm_node, copy=True)
        s_novo, s_ant = novo.sql(dialect="duckdb"), t.sql(dialect="duckdb")
        t = novo
        if s_novo == s_ant:
            break
    return _WS_RE.sub(" ", t.sql(dialect="duckdb")).strip().lower()


def _parse_expr(sql: str):
    try:
        return sqlglot.parse_one(sql, read="duckdb")
    except Exception:
        return None


def _projecoes_lista(select) -> list:
    """[(alias(lower), expressao interna)] na ORDEM do SELECT. [] se nao e SELECT."""
    if not isinstance(select, exp.Select):
        return []
    out = []
    for proj in select.expressions:
        try:
            if isinstance(proj, exp.Star):
                continue
            alias = (proj.alias_or_name or "").lower()
            inner = proj.this if isinstance(proj, exp.Alias) else proj
            out.append((alias, inner))
        except Exception:
            continue
    return out


def _ramos(tree, ctes, metas, prof: int = 0) -> list:
    """[(projecoes_lista, escopo)] — 1 para SELECT, N para UNION/EXCEPT/INTERSECT.
    Cada perna do UNION tem o proprio FROM: a coluna do resultado so recebe unidade/metrica
    quando TODAS as pernas concordam — falso positivo e pior que omissao."""
    if isinstance(tree, exp.Select):
        return [(_projecoes_lista(tree), _escopo_do_select(tree, ctes, metas, prof))]
    lados = [tree.this, tree.args.get("expression")] if hasattr(tree, "args") else []
    out = []
    for lado in lados:
        if lado is not None and isinstance(lado, exp.Expression):
            out.extend(_ramos(lado, ctes, metas, prof))
    return out


def _agg_externo(inner):
    """(FUNC, window) do agregador mais externo da projecao; (None, False) se nao ha."""
    if isinstance(inner, exp.Window):
        return None, True
    agg = inner if isinstance(inner, exp.AggFunc) else next(iter(inner.find_all(exp.AggFunc)), None)
    if agg is None:
        return None, False
    window = any(True for _ in inner.find_all(exp.Window))
    return type(agg).__name__.upper(), window


def _e_leitor(this) -> bool:
    tipo = type(this).__name__.lower()
    if tipo.startswith("read"):
        return True
    if isinstance(this, exp.Anonymous):
        return str(this.name or "").lower().startswith(_LEITORES)
    return False


def _literal_fonte(node) -> str | None:
    lit = next((x for x in node.find_all(exp.Literal) if x.is_string), None)
    return str(lit.this) if lit is not None else None


def _raiz(fonte: str) -> str:
    f = fonte.strip().lower()
    if f.startswith("s3://"):
        return f.rsplit("/", 1)[0] + "/"
    return f


def _meta_da_fonte(fonte: str | None, metas):
    if not fonte:
        return None
    alvo = _raiz(fonte)
    for meta in metas or []:
        src = f"{(meta or {}).get('parquet_source') or ''} {(meta or {}).get('s3_location') or ''}".lower()
        if alvo in src:
            return meta
    return None


# ── contrato ─────────────────────────────────────────────────────────────────
def _metricas(meta) -> list[dict]:
    return [m for m in (((meta or {}).get("semantics") or {}).get("metrics") or []) if isinstance(m, dict)]


def _metrica_por_colunas(cols: set[str], metricas: list[dict]):
    """1o a de conjunto EXATO (metrica composta vence as partes); senao a UNICA que
    intersecta; 0 ou 2+ => None."""
    exatas, hits = [], []
    for m in metricas:
        mcols = {str(c).lower() for c in (m.get("columns") or []) if c}
        if not mcols:
            continue
        if mcols == cols:
            exatas.append(m)
        elif cols & mcols:
            hits.append(m)
    if len(exatas) == 1:
        return exatas[0]
    if not exatas and len(hits) == 1:
        return hits[0]
    return None


def _unit_da_metrica(m: dict):
    return m.get("unit_native") or m.get("unit") or None


def _casts_do_contrato(meta) -> dict:
    """forma canonica do cast DECLARADO -> coluna(lower)."""
    out = {}
    for c in ((meta or {}).get("columns") or []):
        if not isinstance(c, dict):
            continue
        expr = c.get("cast") or c.get("castExpression")
        nome = str(c.get("name") or "").lower()
        if nome and isinstance(expr, str) and expr.strip():
            t = _parse_expr(expr)
            if t is not None:
                out[_canonico(t)] = nome
    return out


def _declara_coluna(meta, nome: str) -> bool:
    if any(nome in {str(x).lower() for x in (mt.get("columns") or [])} for mt in _metricas(meta)):
        return True
    return any(str(col.get("name") or "").lower() == nome
               for col in ((meta or {}).get("columns") or []) if isinstance(col, dict))


def _pattern_da_rota(rota, metas):
    """Spec do sql_pattern da rota, ou None."""
    try:
        if not rota or rota[0] != "pattern" or not rota[1]:
            return None
        ds, _, pname = str(rota[1]).rpartition("/")
        for meta in metas or []:
            if str((meta or {}).get("name")) != ds:
                continue
            spec = (((meta.get("semantics") or {}).get("sql_patterns") or {}).get(pname))
            if isinstance(spec, dict):
                return spec
    except Exception:
        pass
    return None


def _unidade_simples(u) -> str | None:
    if not isinstance(u, str):
        return None
    s = u.strip()
    if not s or any(ch in s for ch in ",();") or " " in s:
        return None
    return s


def _modo_equivalente(expr, metrica: dict):
    """(nome, spec, FUNC) do aggregation_mode AST-equivalente a expressao; None."""
    try:
        canon = _canonico(expr)
    except Exception:
        return None
    modos = metrica.get("aggregation_modes") or {}
    for mname, spec in (modos.items() if isinstance(modos, dict) else []):
        msql = spec.get("sql") if isinstance(spec, dict) else None
        if not isinstance(msql, str):
            continue
        mtree = _parse_expr(msql)
        if mtree is not None and _canonico(mtree) == canon:
            mfunc, _ = _agg_externo(mtree)
            return mname, spec, mfunc
    return None


# ── escopo (origens) ─────────────────────────────────────────────────────────
class _Origem:
    """('fonte', meta) para leitor direto; ('derivada', {col: (expr, escopo)}) para CTE/subquery."""

    __slots__ = ("tipo", "meta", "cols", "casts")

    def __init__(self, tipo, meta=None, cols=None):
        self.tipo = tipo
        self.meta = meta
        self.cols = cols or {}
        self.casts = _casts_do_contrato(meta) if (tipo == "fonte" and meta) else {}

    def declara(self, nome: str) -> bool:
        if self.tipo == "fonte":
            return self.meta is not None and _declara_coluna(self.meta, nome)
        return nome in self.cols


def _ctes(tree) -> dict:
    """alias -> corpo da CTE (Select OU UNION: o corpo de UNION e comum em pivo por fonte)."""
    out = {}
    for c in tree.find_all(exp.CTE):
        if c.alias and isinstance(c.this, exp.Expression):
            out[c.alias.lower()] = c.this
    return out


def _escopo_do_select(select, ctes: dict, metas, prof: int = 0) -> dict:
    """alias(lower) -> _Origem. Alias '' = fonte sem alias."""
    escopo: dict = {}
    if not isinstance(select, exp.Select) or prof > _MAX_PROFUNDIDADE:
        return escopo
    partes = []
    fr = select.args.get("from_") if select.args.get("from_") is not None else select.args.get("from")
    if fr is not None:
        partes.append(fr)
    partes.extend(select.args.get("joins") or [])
    for parte in partes:
        for sub in parte.find_all(exp.Subquery):
            if isinstance(sub.this, exp.Select):
                cols = _cols_derivadas(sub.this, ctes, metas, prof + 1)
                escopo[(sub.alias or "").lower()] = _Origem("derivada", cols=cols)
        for tb in parte.find_all(exp.Table):
            alias = (tb.alias or "").lower()
            this = tb.this
            if _e_leitor(this):
                escopo[alias] = _Origem("fonte", meta=_meta_da_fonte(_literal_fonte(this), metas))
                continue
            nome = (tb.name or "").lower()
            if nome in ctes:
                escopo[alias or nome] = _Origem("derivada", cols=_cols_derivadas(ctes[nome], ctes, metas, prof + 1))
            elif nome:
                escopo[alias or nome] = _Origem("fonte", meta=None)  # tabela nua: origem desconhecida
    # `metas` ja e o conjunto de contratos que o SQL toca: com UM contrato e UMA fonte direta
    # sem contrato casado (fixture/URL re-resolvida), a origem e inequivoca
    metas_lst = [m for m in (metas or []) if isinstance(m, dict)]
    fontes = [o for o in escopo.values() if o.tipo == "fonte"]
    if len(metas_lst) == 1 and len(fontes) == 1 and fontes[0].meta is None and prof == 0:
        fontes[0].meta = metas_lst[0]
        fontes[0].casts = _casts_do_contrato(metas_lst[0])
    return escopo


def _cols_derivadas(corpo, ctes, metas, prof) -> dict:
    """alias -> LISTA de (expressao, escopo) — uma entrada por perna quando o corpo e UNION.
    A coluna so recebe unidade/metrica se as pernas concordarem (ver _tipo_coluna)."""
    out: dict = {}
    for lista, esc in _ramos(corpo, ctes, metas, prof):
        for i, (alias, expr) in enumerate(lista):
            chave = alias or (list(out)[i] if i < len(out) else "")
            if not chave:
                continue
            out.setdefault(chave, []).append((expr, esc))
    return out


# ── tipagem recursiva ────────────────────────────────────────────────────────
class _Tipo:
    __slots__ = ("unit", "tier", "metric", "kind", "agg", "additive", "sources", "ratio", "count", "window", "puro",
                 "meta", "conhecida", "modo")

    def __init__(self):
        self.modo = None
        self.unit = None
        self.tier = "unknown"
        self.metric = None       # dict da metrica (ou None)
        self.kind = None
        self.agg = None
        self.additive = False
        self.sources: set = set()
        self.ratio = False
        self.count = False
        self.window = False
        self.puro = True
        self.meta = None
        self.conhecida = False   # a expressao resolveu ate uma origem/metrica


def _resolver_origem(col: exp.Column, escopo: dict):
    tab = (col.table or "").lower()
    if tab:
        return escopo.get(tab)
    if len(escopo) == 1:
        return next(iter(escopo.values()))
    nome = col.name.lower()
    cands = [o for o in escopo.values() if o.declara(nome)]
    return cands[0] if len(cands) == 1 else None


def _tipo_coluna(col: exp.Column, escopo: dict, prof: int) -> _Tipo:
    t = _Tipo()
    nome = col.name.lower()
    t.sources = {nome}
    origem = _resolver_origem(col, escopo)
    if origem is None:
        return t
    if origem.tipo == "derivada":
        alternativas = origem.cols.get(nome)
        if not alternativas:
            return t
        tipos = [_tipo(expr, esc, prof + 1) for expr, esc in alternativas]
        base = tipos[0]
        for outro in tipos[1:]:  # pernas de UNION: unidade e metrica conciliadas SEPARADAMENTE
            if outro.unit != base.unit:
                base.unit, base.tier, base.modo = None, "unknown", None
            if outro.metric is not base.metric and (
                    not outro.metric or not base.metric or outro.metric.get("id") != base.metric.get("id")):
                base.metric, base.modo = None, None
            base.additive = base.additive and outro.additive
            base.conhecida = base.conhecida and outro.conhecida
        return base
    if origem.meta is None:
        return t
    t.meta = origem.meta
    t.conhecida = True
    metrica = _metrica_por_colunas({nome}, _metricas(origem.meta))
    if metrica is not None:
        t.metric = metrica
        t.kind = (str(metrica.get("quantity_kind") or "").lower() or None)
        t.unit = _unit_da_metrica(metrica)
        t.tier = "column_native" if t.unit else "unknown"
        t.additive = _somavel(metrica)
    return t


def _somavel(metrica) -> bool:
    try:
        from mcp_tiago_dados_abertos.motor.semantics_engine import _summable_core

        return _summable_core(metrica) is True
    except Exception:
        return False


def _tipo_cast_do_contrato(expr, escopo: dict, prof: int):
    """Se a expressao e o cast DECLARADO no contrato para uma coluna, e essa coluna."""
    try:
        canon = _canonico(expr)
    except Exception:
        return None
    for o in escopo.values():
        if o.tipo == "fonte" and o.casts and canon in o.casts:
            nome = o.casts[canon]
            col = exp.Column(this=exp.to_identifier(nome))
            # forca a origem: coluna qualificada pelo alias desta fonte
            for alias, oo in escopo.items():
                if oo is o and alias:
                    col.set("table", exp.to_identifier(alias))
                    break
            return _tipo_coluna(col, {k: v for k, v in escopo.items() if v is o} or escopo, prof + 1)
    return None


def _passthrough(node) -> exp.Expression | None:
    """Nos transparentes para unidade e aditividade."""
    if isinstance(node, (exp.Paren, exp.Alias)):
        return node.this
    if isinstance(node, (exp.Cast, exp.TryCast)) and isinstance(node.to, exp.DataType):
        if node.to.this in _NUMERIC_TYPES:
            return node.this
    if isinstance(node, exp.Round):
        return node.this
    if isinstance(node, exp.Coalesce):
        return node.this
    if isinstance(node, exp.Mul):
        if _is_num(node.expression, 1):
            return node.this
        if _is_num(node.this, 1):
            return node.expression
    return None


def _e_literal_num(node) -> bool:
    if isinstance(node, exp.Paren):
        return _e_literal_num(node.this)
    if isinstance(node, exp.Neg):
        return _e_literal_num(node.this)
    return isinstance(node, exp.Literal) and not node.is_string


def _tipo(expr, escopo: dict, prof: int = 0) -> _Tipo:
    t = _Tipo()
    if expr is None or prof > _MAX_PROFUNDIDADE:
        return t
    try:
        return _tipo_inner(expr, escopo, prof)
    except Exception:
        return t


def _tipo_inner(expr, escopo: dict, prof: int) -> _Tipo:
    t = _Tipo()
    cast = _tipo_cast_do_contrato(expr, escopo, prof) if not isinstance(expr, (exp.Column, exp.Literal)) else None
    if cast is not None:
        return cast
    if isinstance(expr, exp.Column):
        return _tipo_coluna(expr, escopo, prof)
    if isinstance(expr, exp.Literal):
        return t
    pt = _passthrough(expr)
    if pt is not None:
        return _tipo(pt, escopo, prof + 1)
    if isinstance(expr, exp.Case):
        # pivo por ano: CASE WHEN ano = 2021 THEN v [ELSE 0] END — ramos NAO-literais com a
        # MESMA unidade => transparente; unidades diferentes => desconhecida (fail-closed)
        ramos = [i.args.get("true") for i in expr.args.get("ifs") or []] + [expr.args.get("default")]
        ramos = [r for r in ramos if r is not None and not _e_literal_num(r) and not isinstance(r, exp.Null)]
        if not ramos:
            return t
        tipos = [_tipo(r, escopo, prof + 1) for r in ramos]
        base = tipos[0]
        def _mesma_metrica(x: _Tipo) -> bool:
            if x.metric is base.metric:
                return True
            return bool(x.metric and base.metric and x.metric.get("id") == base.metric.get("id"))

        if all(x.unit == base.unit and _mesma_metrica(x) for x in tipos[1:]):
            base.sources = set().union(*(x.sources for x in tipos))
            return base
        t.sources = set().union(*(x.sources for x in tipos))
        t.conhecida = any(x.conhecida for x in tipos)
        return t
    if isinstance(expr, exp.Window):
        inner = _tipo(expr.this, escopo, prof + 1)
        inner.window = True
        inner.additive = False
        return inner
    if isinstance(expr, exp.Filter):
        inner = _tipo(expr.this, escopo, prof + 1)
        inner.puro = False
        inner.additive = False
        inner.unit, inner.tier = None, "unknown"
        return inner
    if isinstance(expr, exp.Count):
        t.count = True
        t.agg = "COUNT"
        t.tier = "count"
        t.additive = not any(True for _ in expr.find_all(exp.Distinct)) and not expr.args.get("distinct")
        t.sources = {c.name.lower() for c in expr.find_all(exp.Column) if c.name}
        t.conhecida = True
        return t
    if isinstance(expr, exp.AggFunc):
        arg = expr.this
        distinct = bool(expr.args.get("distinct")) or isinstance(arg, exp.Distinct) or any(
            True for _ in expr.find_all(exp.Distinct))
        alvo = arg.expressions[0] if isinstance(arg, exp.Distinct) and arg.expressions else arg
        inner = _tipo(alvo, escopo, prof + 1)
        func = type(expr).__name__.upper()
        inner.agg = func
        if distinct:
            inner.puro = False
            inner.additive = False
            inner.unit, inner.tier = None, "unknown"
            return inner
        inner.additive = func == "SUM" and inner.additive and inner.puro
        return _com_modo_multi(_expansoes(expr, escopo), inner)
    if isinstance(expr, (exp.Add, exp.Sub)):
        a, b = _tipo(expr.this, escopo, prof + 1), _tipo(expr.expression, escopo, prof + 1)
        if _e_literal_num(expr.expression):
            return a
        if _e_literal_num(expr.this):
            return b
        t.sources = a.sources | b.sources
        t.agg = a.agg or b.agg
        t.conhecida = a.conhecida and b.conhecida
        t.puro = a.puro and b.puro
        if a.unit and a.unit == b.unit:
            t.unit, t.tier = a.unit, "column_native" if "column_native" in (a.tier, b.tier) else a.tier
            t.metric = a.metric if a.metric is b.metric else None
            t.kind = a.kind if a.kind == b.kind else None
            t.additive = a.additive and b.additive
            t.meta = a.meta
        return _com_modo_multi(_expansoes(expr, escopo), t)
    if isinstance(expr, (exp.Mul, exp.Div)):
        pct, nucleo = _desembrulha_100(expr)
        if isinstance(nucleo, exp.Div) and not _e_literal_num(nucleo.expression) and not _e_literal_num(nucleo.this):
            a, b = _tipo(nucleo.this, escopo, prof + 1), _tipo(nucleo.expression, escopo, prof + 1)
            t.ratio = True
            t.sources = a.sources | b.sources
            t.agg = a.agg or b.agg
            t.conhecida = a.conhecida or b.conhecida
            if pct:
                t.unit = "%"
                # share = agregado / agregado NESTA expressao; razao de colunas (mesmo que
                # derivadas de agregados numa CTE) e percentual estrutural (ast_pct)
                agg_aqui = any(True for _ in nucleo.this.find_all(exp.AggFunc)) and \
                    any(True for _ in nucleo.expression.find_all(exp.AggFunc))
                t.tier = "ast_share" if agg_aqui else "ast_pct"
            return t
        # fator literal: escala muda a unidade (desconhecida), aditividade se mantem
        base = _tipo(expr.expression if _e_literal_num(expr.this) else expr.this, escopo, prof + 1)
        t.sources, t.agg, t.metric, t.kind, t.meta = base.sources, base.agg, base.metric, base.kind, base.meta
        t.additive, t.puro, t.conhecida = base.additive, base.puro, base.conhecida
        t.unit, t.tier = None, "unknown"
        return _com_modo_multi(_expansoes(expr, escopo), t)
    # qualquer outra funcao/CASE (REPLACE nao-declarado, regexp, CASE...): a UNIDADE e
    # desconhecida (transformacao nao provada), mas a METRICA continua identificavel pelas
    # colunas-fonte quando todas vem da MESMA origem — unidade e legitimidade do agregado
    # sao decisoes separadas.
    cols = [c for c in expr.find_all(exp.Column) if c.name]
    t.sources = {c.name.lower() for c in cols}
    t.agg, t.window = _agg_externo(expr)
    origens = {id(o): o for o in (_resolver_origem(c, escopo) for c in cols) if o is not None}
    if cols and len(origens) == 1:
        o = next(iter(origens.values()))
        if o.tipo == "fonte" and o.meta is not None:
            t.meta = o.meta
            t.conhecida = True
            metrica = _metrica_por_colunas(set(t.sources), _metricas(o.meta))
            if metrica is not None:
                t.metric = metrica
                t.kind = (str(metrica.get("quantity_kind") or "").lower() or None)
                t.additive = _somavel(metrica)
    return t


def _desembrulha_100(expr):
    """(e_percentual, nucleo): reconhece 100 * (a/b), (a/b) * 100, (a*100)/b, (100*a)/b."""
    if isinstance(expr, exp.Mul):
        if _is_num(expr.expression, 100) and isinstance(expr.this, (exp.Div, exp.Paren)):
            n = expr.this.this if isinstance(expr.this, exp.Paren) else expr.this
            return True, n
        if _is_num(expr.this, 100) and isinstance(expr.expression, (exp.Div, exp.Paren)):
            n = expr.expression.this if isinstance(expr.expression, exp.Paren) else expr.expression
            return True, n
        return False, expr
    if isinstance(expr, exp.Div):
        num = expr.this.this if isinstance(expr.this, exp.Paren) else expr.this
        if isinstance(num, exp.Mul) and (_is_num(num.expression, 100) or _is_num(num.this, 100)):
            base = num.this if _is_num(num.expression, 100) else num.expression
            return True, exp.Div(this=base, expression=expr.expression)
        return False, expr
    return False, expr


def _expansoes(expr, escopo: dict, prof: int = 0, max_alt: int = 6) -> list:
    """Expressoes com as colunas de CTE/subquery substituidas pela expressao de ORIGEM — UMA
    por perna quando a CTE e UNION. Sem isto, `soma/1000` (onde soma = SUM(col) numa CTE) nunca
    casa com o aggregation_mode do contrato, que fala da coluna original."""
    if prof > _MAX_PROFUNDIDADE or expr is None:
        return [expr]
    alvos = []
    for col in expr.find_all(exp.Column):
        origem = _resolver_origem(col, escopo)
        if origem is not None and origem.tipo == "derivada" and origem.cols.get(col.name.lower()):
            alvos.append(len(origem.cols[col.name.lower()]))
    n = max(alvos) if alvos else 1
    saidas = []
    for k in range(min(n, max_alt)):

        def _t(node, _k=k):
            if isinstance(node, exp.Column):
                origem = _resolver_origem(node, escopo)
                if origem is not None and origem.tipo == "derivada":
                    alts = origem.cols.get(node.name.lower())
                    if alts:
                        inner, esc = alts[min(_k, len(alts) - 1)]
                        return _expansoes(inner, esc, prof + 1, max_alt)[0]
                return exp.Column(this=exp.to_identifier(node.name.lower()))
            return node

        try:
            saidas.append(expr.transform(_t, copy=True))
        except Exception:
            saidas.append(expr)
    return saidas or [expr]


def _com_modo_multi(expansoes: list, t: _Tipo) -> _Tipo:
    """Tenta o modo do contrato em CADA expansao (pernas do UNION): a unidade so vale se TODAS
    concordarem; a metrica cai se diferir (4 fontes = 4 metricas, mesma unidade)."""
    resultados = [_com_modo(e, _copia(t)) for e in expansoes]
    base = resultados[0]
    for outro in resultados[1:]:
        if outro.unit != base.unit:
            base.unit, base.tier, base.modo = t.unit, t.tier, None
            return base
        if outro.metric is not base.metric and (
                not outro.metric or not base.metric or outro.metric.get("id") != base.metric.get("id")):
            base.metric, base.modo = None, None
        base.additive = base.additive and outro.additive
    return base


def _copia(t: _Tipo) -> _Tipo:
    novo = _Tipo()
    for campo in _Tipo.__slots__:
        setattr(novo, campo, getattr(t, campo))
    return novo


def _com_modo(expr, t: _Tipo) -> _Tipo:
    """Modo do contrato AST-equivalente a expressao: o CONTRATO declara unidade E aditividade."""
    if t.metric is None and t.meta is not None:
        # metrica pelas colunas DESTA expressao (nao de t.sources): numa CTE de UNION cada
        # perna expande para a sua propria coluna, e o modo do contrato e o dela.
        cols_expr = {c.name.lower() for c in expr.find_all(exp.Column) if c.name} or set(t.sources)
        # tambem casa a metrica COMPOSTA (geracao_total = hidr+term+eol+solar) pelo conjunto exato
        composta = _metrica_por_colunas(cols_expr, _metricas(t.meta))
        if composta is not None:
            t.metric = composta
            t.kind = (str(composta.get("quantity_kind") or "").lower() or None)
            if t.unit is None:
                t.unit = _unit_da_metrica(composta)
                t.tier = "column_native" if t.unit else t.tier
            t.additive = t.additive or (_somavel(composta) and t.agg == "SUM" and t.puro and not t.window)
    if t.metric is None:
        return t
    modo = _modo_equivalente(expr, t.metric)
    if modo is None:
        return t
    t.unit = modo[1].get("unit") or None
    t.tier = "mode_ast"
    t.additive = modo[2] == "SUM" and not t.window and t.puro
    t.agg = t.agg or modo[2]
    t.conhecida = True
    t.modo = modo[0]
    return t


# ── principal ────────────────────────────────────────────────────────────────
def _desconhecida(nome: str) -> dict:
    return {"name": nome, "role": "unknown", "agg": None, "sources": [], "metric": None, "mode": None,
            "quantity_kind": None, "unit": None, "unit_tier": "unknown", "additive": False}


def _coluna(nome: str, inner, rows, idx: int, escopo: dict) -> dict:
    col = _desconhecida(nome)
    if inner is None:
        return col
    t = _tipo(inner, escopo)
    func, window = _agg_externo(inner)
    col["agg"] = t.agg or func
    col["sources"] = sorted(t.sources or {c.name.lower() for c in inner.find_all(exp.Column) if c.name})
    if t.metric is not None:
        col["metric"] = t.metric.get("id") or None
        col["quantity_kind"] = t.kind
    col["mode"] = t.modo
    nome_sa = _sem_acento(nome)

    if _TEMPORAL_COL_RE.search(nome_sa) or (rows and _coluna_iso(rows, idx)):
        col["role"] = "temporal"
        return col
    if t.count:
        col.update(role="measure", quantity_kind="count", unit=None, unit_tier="count", additive=t.additive)
        return col
    if t.ratio:
        col.update(role="ratio", unit=t.unit, unit_tier=t.tier if t.unit else "unknown")
        return col
    # MIN/MAX/arg_min sobre coluna SEM metrica (ex.: MIN(id_subsistema) numa subquery de
    # de-duplicacao) continua DIMENSAO — so SUM/AVG/COUNT ou metrica/unidade provam medida
    e_medida = (t.metric is not None or (t.conhecida and t.unit is not None)
                or (t.agg in ("SUM", "AVG") and bool(t.sources)))
    if not e_medida:
        if _RELATIVA_COL_RE.search(nome_sa):
            col.update(role="ratio", unit_tier="name_hint")
        else:
            col["role"] = "dimension"
        return col
    col["role"] = "ratio" if (t.kind in _RATIO_KINDS) else "measure"
    col["unit"], col["unit_tier"] = t.unit, (t.tier if t.unit else "unknown")
    col["additive"] = bool(t.additive) and not window
    if col["role"] == "ratio" and col["unit"] is None and t.kind in {"percent", "percentage"}:
        col["unit"] = "%"
    if col["unit"] == "%":
        col["role"] = "ratio"
    return col


def _coluna_multi(nome: str, idx: int, ramos: list, rows) -> dict:
    """Tipa a coluna em CADA perna (alias, senao POSICAO — pernas de UNION costumam vir sem
    alias) e concilia: unidade/metrica so sobrevivem se TODAS as pernas concordarem."""
    if not ramos:
        return _desconhecida(nome)
    tipadas = []
    for lista, escopo in ramos:
        expr = None
        for alias, inner in lista:
            if alias == nome.lower():
                expr = inner
                break
        if expr is None and idx < len(lista):
            expr = lista[idx][1]
        tipadas.append(_coluna(nome, expr, rows, idx, escopo))
    base = dict(tipadas[0])
    if len(tipadas) == 1:
        return base
    # UNIDADE e METRICA sao conciliadas SEPARADAMENTE: as 4 pernas de "geracao por fonte"
    # tem metricas diferentes (hidraulica/termica/eolica/solar) mas a MESMA unidade (GWh) —
    # zerar a unidade por causa da metrica seria omissao desnecessaria.
    if any(o["unit"] != base["unit"] for o in tipadas[1:]):
        base.update(unit=None, unit_tier="unknown", mode=None)
    if any(o["metric"] != base["metric"] for o in tipadas[1:]):
        base.update(metric=None, mode=None)
        if any(o["quantity_kind"] != base["quantity_kind"] for o in tipadas[1:]):
            base["quantity_kind"] = None
    if any(not o["additive"] for o in tipadas[1:]):
        base["additive"] = False
    if any(o["role"] != base["role"] for o in tipadas[1:]):
        base["role"] = "unknown"
    return base


def result_schema(sql, columns, metas, *, rota=None, rows=None) -> dict:
    """Tipagem do resultado. Nunca lanca; colunas sem prova ficam role='unknown'."""
    sql = sql if isinstance(sql, str) else ""
    cols = [str(c) if c is not None else "" for c in (columns or [])]
    qid = "q_" + hashlib.sha1(
        _WS_RE.sub(" ", sql).strip().lower().encode("utf-8"), usedforsecurity=False).hexdigest()[:10]
    out = {
        "query_id": qid,
        "route": {"tier": "adhoc", "pattern": None},
        "datasets": [str(m.get("name")) for m in (metas or []) if isinstance(m, dict) and m.get("name")],
        "columns": [],
    }
    try:
        spec = _pattern_da_rota(rota, metas)
        pattern_unit = None
        if rota and rota[0] == "pattern":
            out["route"] = {"tier": "pattern", "pattern": rota[1]}
            pattern_unit = _unidade_simples((spec or {}).get("output_unit"))
        tree = _parse_expr(sql)
        ramos = _ramos(tree, _ctes(tree) if tree is not None else {}, metas) if tree is not None else []
        for i, nome in enumerate(cols):
            try:
                out["columns"].append(_coluna_multi(nome, i, ramos, rows))
            except Exception:
                out["columns"].append(_desconhecida(nome))
        # output_unit do pattern: SO quando o resultado tem exatamente 1 medida nao-COUNT
        if pattern_unit:
            medidas = [c for c in out["columns"] if c["role"] in ("measure", "ratio") and c["unit_tier"] != "count"]
            if len(medidas) == 1:
                m = medidas[0]
                m.update(unit=pattern_unit, unit_tier="pattern_output_unit")
                if pattern_unit == "%":
                    m["role"] = "ratio"
    except Exception:
        out["columns"] = [_desconhecida(n) for n in cols]
    return out


def result_schema_do_sql(sql, columns, catalog, *, rows=None, rota=None):
    """result_schema com rota (pattern/adhoc) resolvida no catalogo — ou a rota EXPLICITA do
    planejador — e datasets pelo SQL. None se nao ha colunas ou se algo falhar."""
    try:
        if not columns:
            return None
        from mcp_tiago_dados_abertos.resposta.provenance import datasets_do_sql, rota_do_sql

        metas = datasets_do_sql(sql, catalog or {})
        if rota is None:
            rota = rota_do_sql(sql, catalog or {})
        return result_schema(sql, columns, metas, rota=rota, rows=rows)
    except Exception:
        return None


def bloco_result_schema(rs) -> str:
    try:
        if not rs:
            return ""
        import json

        return "\n\n```result-schema\n" + json.dumps(rs, ensure_ascii=False) + "\n```"
    except Exception:
        return ""


def com_fatos_tipados(facts, rs, *, truncated: bool = False, motivos=None):
    """Enriquece o dict computed-facts com query_id + facts tipados + abstentions + coverage."""
    try:
        if not isinstance(facts, dict) or not facts:
            return facts
        from mcp_tiago_dados_abertos.resposta.fact_packet import fact_packet_completo

        pacote = fact_packet_completo(facts, rs, truncated=truncated, motivos=motivos)
        if not pacote["facts"] and not pacote["abstentions"]:
            return facts
        base = {"query_id": (rs or {}).get("query_id"), "facts": pacote["facts"],
                "abstentions": pacote["abstentions"], "coverage": pacote["coverage"]}
        # Com fatos TIPADOS, o legado (sum_of_rows/by_dimension/by_period/by_rows) e duplicata:
        # sai do bloco. Sem fatos tipados ele ainda e a unica cobertura e continua.
        return base if pacote["facts"] else {**base, **facts}
    except Exception:
        return facts
