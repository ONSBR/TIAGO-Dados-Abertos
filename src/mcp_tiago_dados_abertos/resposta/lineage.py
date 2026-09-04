# -*- coding: utf-8 -*-
"""Lineage minimo do SELECT externo via AST (sqlglot, dialeto duckdb).

Responde, por projecao: qual funcao agregadora (SUM/COUNT/AVG/...), quais
colunas-fonte ela referencia e se e janela (OVER). Dai decorre a ADITIVIDADE
por coluna de resultado — SUM sobre coluna de metrica de FLUXO do contrato
(_summable_core = fonte unica da somabilidade) ou COUNT — e os rotulos de
rollup declarados no contrato (has_total_row / exclude_in_aggregation).

Revisao: a regex anterior (_AGG_COL_RE) perdia t.col, CAST,
FILTER e janela, e casava agregado em comentario/CTE. Lineage nao desce em
subquery: projecao que e coluna pura de subquery fica func=None (conservador).
Nunca lanca.
"""
from __future__ import annotations

import sqlglot
from sqlglot import exp


def projecoes_agregadas(sql: str | None) -> list[dict]:
    """[{alias, func, cols, window}] das projecoes do SELECT externo."""
    if not sql or not isinstance(sql, str) or not sql.strip():
        return []
    try:
        tree = sqlglot.parse_one(sql, read="duckdb")
    except Exception:
        return []
    if not isinstance(tree, exp.Select):
        return []
    out: list[dict] = []
    for proj in tree.expressions:
        try:
            alias = proj.alias_or_name or ""
            inner = proj.this if isinstance(proj, exp.Alias) else proj
            window = isinstance(inner, exp.Window) or any(True for _ in inner.find_all(exp.Window))
            agg = inner if isinstance(inner, exp.AggFunc) else next(iter(inner.find_all(exp.AggFunc)), None)
            func = type(agg).__name__.upper() if agg is not None else None
            cols = sorted({c.name for c in inner.find_all(exp.Column) if c.name})
            distinct = bool(agg is not None and (
                agg.args.get("distinct") or any(True for _ in agg.find_all(exp.Distinct))))
            out.append({"alias": alias, "func": func, "cols": cols, "window": window, "distinct": distinct})
        except Exception:
            continue
    return out


def _metricas(metas) -> list[dict]:
    res: list[dict] = []
    for meta in metas or []:
        for m in (((meta or {}).get("semantics") or {}).get("metrics") or []):
            if isinstance(m, dict):
                res.append(m)
    return res


def colunas_aditivas(sql: str | None, metas) -> set[str]:
    """Aliases do resultado cuja SOMA/PARTICIPACAO e legitima: COUNT (sem janela)
    ou SUM (sem janela) sobre coluna de metrica somavel do contrato."""
    try:
        from mcp_tiago_dados_abertos.motor.semantics_engine import _summable_core
    except Exception:  # pragma: no cover
        _summable_core = None  # type: ignore
    metricas = _metricas(metas)
    out: set[str] = set()
    for p in projecoes_agregadas(sql):
        if p["window"] or not p["alias"] or p.get("distinct"):
            continue  # janela e DISTINCT nunca sao aditivos entre grupos
        if p["func"] == "COUNT":
            out.add(p["alias"])
            continue
        if p["func"] != "SUM" or _summable_core is None:
            continue
        cols = {c.lower() for c in p["cols"]}
        for m in metricas:
            mcols = {str(c).lower() for c in (m.get("columns") or []) if c}
            if cols & mcols:
                try:
                    if _summable_core(m):
                        out.add(p["alias"])
                        break
                except Exception:
                    continue
    return out


def rotulos_rollup(metas) -> set[str]:
    """Rotulos (lower) de linhas de TOTAL declaradas no contrato — nunca entram
    em soma/participacao (dupla contagem: SIN + subsistemas)."""
    out: set[str] = set()
    for meta in metas or []:
        rg = ((meta or {}).get("semantics") or {}).get("row_grain") or {}
        htr = rg.get("has_total_row") or {}
        for v in list(htr.get("values") or []) + list(rg.get("exclude_in_aggregation") or []):
            s = str(v).strip().lower()
            if s:
                out.add(s)
    return out
