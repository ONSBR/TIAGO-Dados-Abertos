# -*- coding: utf-8 -*-
"""Computed facts v4.3: trust-the-contract + whitelist FLOW_KINDS + by_period + rank.

v4.3 DESIGN CHANGES from v4.2:
1. by_period: deltas temporais para serie temporal simples (1 temporal, 0 dims)
   - delta_abs = atual - anterior (sempre que houver anterior)
   - delta_pct = (atual-anterior)/anterior*100 (so se anterior > 0)
   - INVARIANTE: fsum(sums de by_period) == sum_of_rows
2. rank explicito em by_dimension (1-based, ordem sum desc existente)
   - Adiciona "ordered_by": "sum_desc" ao objeto by_dimension
3. Mesmas guards de omission-safe: NaN/Inf/None omite by_period inteiro

v4 DESIGN CHANGES from v3:
1. Uses plano.summable DIRECTLY (no _find_metric fallback)
2. No plano.summable attr or summable=None -> no sum (safe default)
3. Decimal support (DuckDB returns Decimal for DECIMAL columns)
4. Removed _NON_SUMMABLE_KINDS blacklist (now whitelist in semantics_engine)
5. Removed _is_summable/_mode_uses_sum (now only _compute_summable in engine)

KEY PRINCIPLE: The engine is DUMB — it trusts the contract.
The semantic authority is the contract (via plano.summable computed at plan-time).

Emits (when safe to compute):
- primary_metric: {unit}                 -- ALWAYS when plano.unit != ""
- sum_of_rows: {value, unit}             -- grand total; ONLY when plano.summable=True
- mean_of_rows: {value, unit}            -- ONLY in 1D results (not 2D breakdowns)
- by_dimension: {column, unit, groups[], ordered_by}  -- breakdown com rank e pct
- by_period: {column, unit, periods[]}   -- deltas temporais para serie simples

Omission-safe: never raises, rejects NaN/Inf, omits when nothing is provable.
"""

from __future__ import annotations

import datetime as _dt
import math
import re
from decimal import Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Any, Optional


# ── Temporal/ID/relative column patterns (reused from db._participacao_footer) ─
_TEMPORAL_COL_RE = re.compile(
    r"(?:^|_)(din|dat|data|date|periodo|mes|ano|dia|instante|competencia|hora|hor|time|timestamp|semana|week)(?:_|$)",
    re.I,
)
_ID_COL_RE = re.compile(r"(?:^|_)(cod|codigo|id|num|ano|year|ceg|ranking)(?:_|$)", re.I)
_RELATIVA_COL_RE = re.compile(r"(pct|percent|taxa|indice|fator|ratio|propor)", re.I)


def _is_finite(val: float) -> bool:
    """Check if value is finite (not NaN, not Inf)."""
    return isinstance(val, (int, float)) and math.isfinite(val)


def _extract_numeric(val: Any) -> Optional[float]:
    """Extract numeric value from a cell, returning None if not numeric.

    v4 FIX: Supports decimal.Decimal (DuckDB returns Decimal for DECIMAL columns).
    """
    if val is None:
        return None
    if isinstance(val, bool):
        return None
    # v4: Handle Decimal (DuckDB returns Decimal for DECIMAL columns)
    if isinstance(val, Decimal):
        try:
            f = float(val)
            if not math.isfinite(f):
                return None
            return f
        except (ValueError, TypeError, OverflowError):
            return None
    if isinstance(val, (int, float)):
        if not math.isfinite(val):
            return None  # reject NaN/Inf
        return float(val)
    # Try to parse string
    if isinstance(val, str):
        try:
            v = float(val.replace(",", "."))
            if not math.isfinite(v):
                return None
            return v
        except (ValueError, TypeError):
            return None
    return None


def _find_measure_column_indices(columns: list[str], rows: list[tuple]) -> list[int]:
    """Find indices of MEASURE columns in the result using heuristics.

    A measure column is:
    - NOT temporal (din_*, dat_*, mes, ano, etc.)
    - NOT an ID/code column (cod_*, id_*, num_*, etc.)
    - NOT a relative/percentage column (pct, percent, taxa, etc.)
    - Has AT LEAST ONE numeric (non-None, finite) value in the rows

    Returns list of indices that are candidate measure columns.
    """
    if not columns or not rows:
        return []

    measure_indices = []
    for i, col in enumerate(columns):
        col_str = str(col) if col else ""

        # Skip temporal columns
        if _TEMPORAL_COL_RE.search(col_str):
            continue
        # Skip ID/code columns
        if _ID_COL_RE.search(col_str):
            continue
        # Skip relative/percentage columns
        if _RELATIVA_COL_RE.search(col_str):
            continue

        # Check if at least one value in this column is numeric
        # (tolerates NaN/Inf/None, only requires some valid values)
        has_numeric = False
        has_non_numeric_text = False
        for row in rows:
            if not isinstance(row, (list, tuple)) or len(row) <= i:
                continue
            val = row[i]
            if val is None:
                continue  # None is ok, skip
            num = _extract_numeric(val)
            if num is not None:
                has_numeric = True
            else:
                # Non-numeric text (string that's not a number)
                if isinstance(val, str):
                    has_non_numeric_text = True

        # Include if has numeric values and no non-numeric text
        if has_numeric and not has_non_numeric_text:
            measure_indices.append(i)

    return measure_indices


def _sem_acento(s: str) -> str:
    import unicodedata
    return "".join(ch for ch in unicodedata.normalize("NFD", s) if unicodedata.category(ch) != "Mn")


_ISO_CELL_RE = re.compile(r"^\d{4}-\d{2}(?:-\d{2})?(?:[ T].*)?$")
_ROLLUP_LABEL_RE = re.compile(r"\b(sin|total|geral|brasil|nacional|sistema|todos|todas|consolidado)\b", re.I)


def _coluna_iso(rows: list, i: int) -> bool:
    """Coluna cujo conteudo e data/mes ISO (temporal por CONTEUDO, alias qualquer)."""
    vistos = 0
    for r in rows:
        if not isinstance(r, (list, tuple)) or len(r) <= i or r[i] is None:
            continue
        v = r[i]
        if hasattr(v, "isoformat"):
            vistos += 1
            continue
        if isinstance(v, str) and _ISO_CELL_RE.match(v.strip()):
            vistos += 1
            continue
        return False
    return vistos > 0


def _pct_maior_resto(vals: list[float], total: float) -> list[float]:
    """Percentuais a 1 casa que SOMAM 100.0 exato (metodo do maior resto)."""
    brutos = [1000.0 * v / total for v in vals]
    base = [int(x) for x in brutos]
    falta = 1000 - sum(base)
    ordem = sorted(range(len(vals)), key=lambda k: brutos[k] - base[k], reverse=True)
    for k in ordem[:max(0, falta)]:
        base[k] += 1
    return [b / 10.0 for b in base]


def _totais_do_periodo(cols: list[str], rows, aditivas: set) -> dict:
    """{coluna: soma} das medidas ADITIVAS de uma serie temporal pura. Coluna com celula
    nao-numerica fica de fora. Nunca lanca."""
    out: dict = {}
    for i, col in enumerate(cols):
        if col not in aditivas:
            continue
        vals = [_extract_numeric(r[i]) for r in rows
                if isinstance(r, (list, tuple)) and len(r) > i]
        if not vals or any(v is None for v in vals):
            continue
        total = math.fsum(vals)
        if _is_finite(total):
            out[col] = total
    return out


def facts_by_rows_com_motivo(
    columns: list[str] | None,
    rows: list[tuple] | None,
    *,
    schema,
    truncated: bool = False,
    max_rows: int = 30,
    max_measures: int = 8,  # limite ampliado para cobrir mais medidas na resposta
    exclude_labels=None,
) -> tuple[dict | None, dict]:
    """Participacao % e rank POR LINHA, por medida, a partir do RESULT_SCHEMA.

    O `schema` (result_schema) e a fonte UNICA de papel, aditividade e unidade — antes esta
    funcao redecidia por regex de nome, criando duas verdades sobre "o que e medida" (a mesma
    classe do bug de aditividade). Sem schema nao ha fato: fail-closed.

    Retorna (facts | None, motivos) — motivos = reason_code por medida/geral (telemetria).
    - papel: temporal em qualquer coluna => abstem (share de "um mes" nao e fato);
      dimension => rotulo; measure/ratio => medida; unknown => ignorada.
    - pct SO em medida ADITIVA pelo schema; ratio nunca soma nem tem share (soma de % nao e
      fato); medida nao-aditiva ganha so rank (rankear preco faz sentido, share de preco nao).
    - rollup: rotulo do contrato (exclude_labels) e, como HEURISTICA de abstencao, uma linha
      que vale a soma das outras COM rotulo de total (nunca gera fato; ver backlog
      "declarar has_total_row nos contratos").
    - Rank denso em empate. Percentuais a 1 casa pelo maior resto (somam 100.0). Nunca lanca.
    """
    motivos: dict = {}
    try:
        if truncated:
            return None, {"_": "paginado"}
        if not columns or not rows:
            return None, {"_": "vazio"}
        papeis = {str(c.get("name")): c for c in ((schema or {}).get("columns") or []) if isinstance(c, dict)}
        if not papeis:
            return None, {"_": "sem_schema"}
        cols = [str(c) if c else "" for c in columns]
        papel = [(papeis.get(c) or {}).get("role", "unknown") for c in cols]
        adit_todas = {c for c in cols if (papeis.get(c) or {}).get("additive")}
        if "temporal" in papel:
            # Share ENTRE periodos nao e fato. Mas numa SERIE PURA (so temporal + medidas) o
            # TOTAL DO PERIODO por medida aditiva e — era o numero da frase-tese que morria.
            # Com dimensao junto (grid 2D) nao soma: linha de rollup dobraria o total.
            if any(p == "dimension" for p in papel):
                return None, {"_": "temporal"}
            totais = _totais_do_periodo(cols, rows, adit_todas)
            return (({"totals": totais}, {"_": "serie_temporal"}) if totais
                    else (None, {"_": "temporal"}))
        measure_all = [i for i, p in enumerate(papel) if p in ("measure", "ratio")]
        ratio_cols = {cols[i] for i, p in enumerate(papel) if p == "ratio"}
        adit = {c for c in cols if (papeis.get(c) or {}).get("additive")}
        if not measure_all:
            return None, {"_": "sem_medida"}
        dim_idx = [i for i, p in enumerate(papel) if p == "dimension"]
        if not dim_idx:
            return None, {"_": "sem_dimensao"}
        excl = {str(x).strip().lower() for x in (exclude_labels or set())}
        largura = max(dim_idx + measure_all)
        keep = [
            r for r in rows
            if isinstance(r, (list, tuple)) and len(r) > largura
            and not any(r[d] is not None and str(r[d]).strip().lower() in excl for d in dim_idx)
        ]
        if not (2 <= len(keep) <= max_rows):
            return None, {"_": "n_linhas"}
        labels = [" | ".join(str(r[d]).strip() if r[d] is not None else "" for d in dim_idx) for r in keep]
        if len(set(labels)) != len(labels):
            return None, {"_": "rotulo_repetido"}
        measures: dict = {}
        for mi in measure_all[:max_measures]:
            col = cols[mi]
            vals = [_extract_numeric(r[mi]) for r in keep]
            if any(v is None for v in vals):
                motivos[col] = "celula_nao_numerica"
                continue
            total = math.fsum(vals)
            if not _is_finite(total):
                motivos[col] = "total_invalido"
                continue
            # Rollup NAO declarado no contrato: so abstem com corroboracao dupla —
            # a linha MAXIMA vale a soma das outras E o rotulo tem cara de total
            # (SIN/total/geral/brasil/nacional/sistema). Aritmetica sozinha da
            # falso-positivo legitimo (SE = 50% = soma dos demais); rotulo sozinho
            # e chute. Juntos, a abstencao (omissao) e segura; nunca gera fato errado.
            if len(vals) >= 3:
                kmax = max(range(len(vals)), key=lambda k: vals[k])
                vmax = vals[kmax]
                if (
                    vmax != 0
                    and abs(vmax - (total - vmax)) <= abs(total - vmax) * 0.005
                    and _ROLLUP_LABEL_RE.search(_sem_acento(labels[kmax]))
                ):
                    motivos[col] = "rollup_detectado"
                    continue
            has_neg = any(v < 0 for v in vals)
            relativa = col in ratio_cols
            com_pct = col in adit and not has_neg and total > 0 and not relativa
            pcts = _pct_maior_resto(vals, total) if com_pct else None
            order = sorted(range(len(vals)), key=lambda k: vals[k], reverse=True)
            groups: list[dict] = []
            rank, prev = 0, None
            for k in order:
                if prev is None or vals[k] != prev:
                    rank += 1
                prev = vals[k]
                g: dict = {"label": labels[k], "value": vals[k], "rank": rank}
                if pcts is not None:
                    g["pct"] = pcts[k]
                groups.append(g)
            if col not in adit:
                motivos[col] = "nao_aditiva_sem_pct"
            elif has_neg:
                motivos[col] = "valores_negativos"  # aditiva, mas share de negativo nao e fato
            elif total <= 0:
                motivos[col] = "total_nao_positivo"
            measures[col] = {"total": None if relativa else total, "groups": groups}  # soma de % nao e fato
        if not measures:
            return None, motivos
        return {"by_rows": {"dimension": [cols[d] for d in dim_idx], "measures": measures}}, motivos
    except Exception as e:  # omission-safe
        return None, {"_": f"excecao:{type(e).__name__}"}


def facts_by_rows(columns, rows, **kw) -> dict | None:
    """Atalho: so os fatos (ver facts_by_rows_com_motivo)."""
    return facts_by_rows_com_motivo(columns, rows, **kw)[0]


def _count_dimension_columns(columns: list[str], measure_indices: list[int]) -> int:
    """Count non-temporal, non-measure columns (dimensions).

    Used to detect 1D vs 2D results for mean_of_rows emission.

    ID columns (id_subsistema, cod_agente) ARE dimensions for this purpose,
    even though they're not measure candidates. A breakdown by subsystem
    is a real 2D result where mean over cells is ambiguous.
    """
    count = 0
    for i, col in enumerate(columns):
        if i in measure_indices:
            continue
        col_str = str(col) if col else ""
        # Skip temporal columns (they're not dimensions for this purpose)
        if _TEMPORAL_COL_RE.search(col_str):
            continue
        # Skip percentage/relative columns (they're derived, not dimensions)
        if _RELATIVA_COL_RE.search(col_str):
            continue
        # ID columns (id_subsistema, cod_agente) ARE dimensions - don't skip
        count += 1
    return count


# v4: REMOVED _mode_uses_sum, _is_summable, _find_metric
# The engine now trusts plano.summable computed by _compute_summable in semantics_engine.
# No fallback lookup — if plano.summable is not set, no sum.


def compute_facts(
    columns: list[str] | None,
    rows: list[tuple] | None,
    meta: dict | None,
    plano: Any,  # Plan object with .unit, .mode, .summable
    *,
    truncated: bool = False,  # v3.1: if True, result is a page (not full) → no sum/mean
    executed_sql: str | None = None,  # v4.1: SQL REALMENTE executado (gate fail-closed)
) -> dict | None:
    """Compute deterministic facts from raw result rows.

    v4 DESIGN: Trusts plano.summable DIRECTLY (no fallback to meta lookup).

    The semantic authority is the contract. plano.summable is computed once at
    plan-time by _compute_summable using the whitelist FLOW_KINDS. If plano lacks
    the summable attribute or summable=None, we assume False (safe default).

    Args:
        columns: List of column names from the result
        rows: List of row tuples from the result (raw, not markdown)
        meta: Dataset metadata dict (unused in v4, kept for API compat)
        plano: Plan object from semantics_engine.plan() with .unit, .mode, .summable
        truncated: If True, result is paginated (not all rows) → no sum/mean

    Returns:
        Dict with computed facts, or None if nothing can be computed safely.
        Never raises.

    Output format:
        {
            "primary_metric": {"unit": "<plano.unit>"},
            "sum_of_rows": {"value": <float>, "unit": "<plano.unit>"},   # if summable & !truncated
            "mean_of_rows": {"value": <float>, "unit": "<plano.unit>"}   # if 1D & !truncated
        }
    """
    try:
        return _compute_facts_inner(
            columns, rows, meta, plano, truncated=truncated, executed_sql=executed_sql
        )
    except Exception:
        # Omission-safe: never raise
        return None


def _count_temporal_columns(columns: list[str], measure_indices: list[int]) -> int:
    """Count the number of DISTINCT temporal columns (not boolean).

    v3.1 FIX: ano|mes|valor has 2 temporal columns, not "has_temporal=True".
    Each temporal column counts as one grouping key.
    """
    count = 0
    for i, col in enumerate(columns):
        if i in measure_indices:
            continue
        col_str = str(col) if col else ""
        if _TEMPORAL_COL_RE.search(col_str):
            count += 1
    return count


def _compute_facts_inner(
    columns: list[str] | None,
    rows: list[tuple] | None,
    meta: dict | None,
    plano: Any,
    *,
    truncated: bool = False,
    executed_sql: str | None = None,
) -> dict | None:
    """Inner implementation (can raise, wrapped by compute_facts)."""

    # Guard: need columns and rows
    if not columns or not rows:
        return None

    if not isinstance(columns, (list, tuple)) or not isinstance(rows, (list, tuple)):
        return None

    # ── Get unit/summable from plano (THE FIX: no column-name matching) ──
    plano_unit = ""
    plano_summable: bool | None = None  # None = not set, assume False (safe default)

    # Support both Plan object (has .unit/.mode/.summable) and dict (testing)
    if hasattr(plano, "unit"):
        plano_unit = plano.unit or ""
        # v3.1: use plano.summable if available
        if hasattr(plano, "summable"):
            plano_summable = plano.summable
    elif isinstance(plano, dict):
        plano_unit = plano.get("unit", "") or ""
        if "summable" in plano:
            plano_summable = plano.get("summable")

    # If plano.unit is empty, OMIT the entire block (nothing provable)
    if not plano_unit:
        return None

    # ── Find measure column via heuristics ──
    measure_indices = _find_measure_column_indices(list(columns), list(rows))

    # Ambiguity: 0 or ≥2 candidate measure columns → OMIT (don't guess)
    if len(measure_indices) != 1:
        return None

    col_idx = measure_indices[0]

    # ── Extract numeric values from the measure column ──
    values: list[float] = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) <= col_idx:
            continue
        val = _extract_numeric(row[col_idx])
        if val is not None:
            values.append(val)

    # No valid values - omit
    if not values:
        return None

    # ── Build result ──
    result: dict = {
        "primary_metric": {
            "unit": plano_unit,
        },
    }

    # ── sum_of_rows: only if plano.summable=True AND not truncated ──
    # v4 DESIGN: NO FALLBACK to _find_metric. plano.summable is the authority.
    # If plano lacks summable attr or summable is None/False → no sum (safe default).
    is_summable = plano_summable is True  # Explicit True, not None/False/missing

    # v4.1 GATE fail-closed no SQL EXECUTADO: o plano pode ter escolhido um
    # sql_pattern cujo agregador difere do modo (ex.: mode=total_energy diz SUM
    # mas o pattern executa AVG(GERACAO)). Somar
    # linhas que ja sao MEDIAS e errado. Se o SQL executado nao agrega por SUM
    # (ou contem AVG), nao somar. Sem executed_sql informado, confia no plano
    # (caminho build_from_metrics, que segue o modo).
    if is_summable and executed_sql:
        sql_up = executed_sql.upper()
        if not re.search(r"\bSUM\s*\(", sql_up) or re.search(r"\bAVG\s*\(", sql_up):
            is_summable = False

    # KEY: if truncated, do NOT emit sum_of_rows (it's only the page sum)
    if is_summable and not truncated:
        sum_val = math.fsum(values)
        if _is_finite(sum_val):
            result["sum_of_rows"] = {
                "value": sum_val,
                "unit": plano_unit,
            }

    # ── mean_of_rows: only in 1D results AND not truncated ──
    # 1D = at most ONE grouping key TOTAL.
    # v3.1 FIX: count DISTINCT temporal columns (not boolean).
    # ano|mes|valor → n_temporal=2 → n_grouping=2 → 2D → no mean.
    n_dims = _count_dimension_columns(list(columns), measure_indices)
    n_temporal = _count_temporal_columns(list(columns), measure_indices)
    n_grouping = n_dims + n_temporal

    # KEY: if truncated, do NOT emit mean_of_rows (it's only the page mean)
    if n_grouping <= 1 and not truncated:
        mean_val = math.fsum(values) / len(values)
        if _is_finite(mean_val):
            result["mean_of_rows"] = {
                "value": mean_val,
                "unit": plano_unit,
            }

    # ── by_dimension: breakdown por dimensão com participação % ──────────────
    # v4.2 (Fatia 2): emitir quando TODAS as condições:
    # - summable=True E não-truncado E gate SQL-SUM passa (mesmas condições do sum_of_rows)
    # - EXATAMENTE 1 coluna de dimensão NÃO-temporal no resultado
    # - <= 30 grupos (senão omitir — tabela gigante)
    # - Se algum valor negativo → groups SEM pct (% de negativo não faz sentido)
    if is_summable and not truncated and "sum_of_rows" in result:
        by_dim = _compute_by_dimension(
            list(columns), list(rows), col_idx, measure_indices, plano_unit
        )
        if by_dim:
            result["by_dimension"] = by_dim

    # ── by_period: deltas temporais para serie temporal simples ──────────────
    # v4.3: emitir quando TODAS as condicoes:
    # - summable=True E nao-truncado E gate SQL-SUM passa (mesmas condicoes do sum_of_rows)
    # - EXATAMENTE 1 coluna temporal E ZERO dimensoes nao-temporais
    # - 2 a 40 periodos (fora disso OMITE)
    # - Labels ordenaveis cronologicamente
    # - Nenhum NaN/Inf/None em qualquer celula do grupo
    # - Nenhum periodo duplicado
    # Se ha entidade (dimensao nao-temporal), by_dimension ja cobre e by_period seria ambiguo
    if is_summable and not truncated and "sum_of_rows" in result:
        by_per = _compute_by_period(
            list(columns), list(rows), col_idx, measure_indices, plano_unit
        )
        if by_per:
            result["by_period"] = by_per

    # If only primary_metric (no sum, no mean), still return it
    return result


def _find_non_temporal_dimension_column(
    columns: list[str], measure_indices: list[int]
) -> tuple[int, str] | None:
    """Encontra a ÚNICA coluna de dimensão NÃO-temporal.

    Retorna (índice, nome) ou None se:
    - 0 dimensões não-temporais
    - 2+ dimensões não-temporais (ambíguo)
    """
    candidates: list[tuple[int, str]] = []
    for i, col in enumerate(columns):
        if i in measure_indices:
            continue
        col_str = str(col) if col else ""
        # Skip temporal
        if _TEMPORAL_COL_RE.search(col_str):
            continue
        # Skip percentage/relative
        if _RELATIVA_COL_RE.search(col_str):
            continue
        # Esta é uma dimensão não-temporal
        candidates.append((i, col_str))

    # EXATAMENTE 1 dimensão não-temporal
    if len(candidates) == 1:
        return candidates[0]
    return None


def _compute_by_dimension(
    columns: list[str],
    rows: list[tuple],
    measure_idx: int,
    measure_indices: list[int],
    unit: str,
) -> dict | None:
    """Computa by_dimension: grupos ordenados por sum desc com participacao %, rank.

    INVARIANTE: soma dos groups == sum_of_rows.
    Se algum valor for negativo, omite o pct (% de negativo nao faz sentido).
    Limite: max 30 grupos (senao omite).

    v4.3: adiciona "rank" (1-based) a cada grupo e "ordered_by": "sum_desc" ao objeto.
    """
    # Encontra a dimensao nao-temporal
    dim_info = _find_non_temporal_dimension_column(columns, measure_indices)
    if not dim_info:
        return None  # 0 ou 2+ dimensoes: ambiguo

    dim_idx, dim_col = dim_info

    # Agrupa valores por chave de dimensao
    # Para grid tempoxdim, soma atraves do tempo (valido pois summable=True)
    by_vals: dict[str, list] = {}
    has_negative_cell = False
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) <= max(dim_idx, measure_idx):
            continue
        key = str(row[dim_idx]) if row[dim_idx] is not None else ""
        val = _extract_numeric(row[measure_idx])
        if val is not None:
            by_vals.setdefault(key, []).append(val)
            if val < 0:
                has_negative_cell = True
    # fsum POR GRUPO: acumular com '+' perde precisao em
    # cancelamento catastrofico (1e16 + 1 - 1e16); fsum nos grupos E no total
    # mantem a invariante soma(groups)==sum_of_rows (fsum das mesmas celulas).
    by_key: dict[str, float] = {k: math.fsum(v) for k, v in by_vals.items()}

    if not by_key:
        return None

    # Limite: max 30 grupos
    if len(by_key) > 30:
        return None

    # Negativo por CELULA CRUA: totais por grupo podem
    # compensar (+10,-5 => +5) e esconder negativos — pct seria enganoso.
    has_negative = has_negative_cell or any(v < 0 for v in by_key.values())

    # Ordena por sum desc
    sorted_items = sorted(by_key.items(), key=lambda kv: kv[1], reverse=True)

    # Calcula total para pct
    total = math.fsum(by_key.values())

    # Constroi grupos com rank (1-based)
    groups: list[dict] = []
    rank = 0
    for key, sum_val in sorted_items:
        if not _is_finite(sum_val):
            continue
        rank += 1
        group: dict = {
            "rank": rank,  # v4.3: rank explicito (1-based, ordem sum desc)
            "key": key,
            "sum": sum_val,  # CRU — invariante bit-a-bit com sum_of_rows (arredondar e papel da UI)
        }
        # pct so se nao ha valores negativos e total > 0
        if not has_negative and total > 0 and _is_finite(total):
            pct = (sum_val / total) * 100
            if _is_finite(pct):
                group["pct"] = round(pct, 1)  # 1 casa decimal
        groups.append(group)

    if not groups:
        return None

    return {
        "column": dim_col,
        "unit": unit,
        "ordered_by": "sum_desc",  # v4.3: torna a ordem explicita
        "groups": groups,
    }


def _find_temporal_column(
    columns: list[str], measure_indices: list[int]
) -> tuple[int, str] | None:
    """Encontra a UNICA coluna temporal no resultado.

    Retorna (indice, nome) ou None se:
    - 0 colunas temporais
    - 2+ colunas temporais (ambiguo para by_period)
    """
    candidates: list[tuple[int, str]] = []
    for i, col in enumerate(columns):
        if i in measure_indices:
            continue
        col_str = str(col) if col else ""
        if _TEMPORAL_COL_RE.search(col_str):
            candidates.append((i, col_str))

    # EXATAMENTE 1 coluna temporal
    if len(candidates) == 1:
        return candidates[0]
    return None


def _try_parse_sortable_label(label: str) -> tuple | None:
    """Parse ESTRITO de label temporal para ordenacao cronologica.

    Retorna tupla ordenavel ou None. Formatos INTEGRALMENTE reconhecidos
    (fullmatch — sufixo/lixo/timezone invalida; calendario e faixas de hora
    validados via datetime, entao 2025-99-99 e 2025-01-01 27:00 sao None):
    - datetime: 2025-01-15T10:00:00 ou 2025-01-15 10:00:00 (DuckDB usa ESPACO;
      segundos e fracao opcionais)
    - date: 2025-01-15
    - Ano-mes: 2025-01, 2025/01
    - Ano: 2025
    Na duvida -> None (o chamador omite by_period inteiro).
    """
    if not label or not isinstance(label, str):
        return None

    s = label.strip()

    # datetime/date completo, separador T OU espaco (DuckDB emite espaco)
    m = re.fullmatch(
        r"(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,6}))?)?)?", s
    )
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        hh = int(m.group(4)) if m.group(4) else 0
        mi = int(m.group(5)) if m.group(5) else 0
        ss = int(m.group(6)) if m.group(6) else 0
        frac = int((m.group(7) or "0").ljust(6, "0"))
        try:
            _dt.datetime(y, mo, d, hh, mi, ss)  # valida calendario e faixas
        except ValueError:
            return None
        return (y, mo, d, hh, mi, ss, frac)

    # Ano-mes: 2025-01 ou 2025/01
    m = re.fullmatch(r"(\d{4})[-/](\d{2})", s)
    if m:
        mo = int(m.group(2))
        if not 1 <= mo <= 12:
            return None
        return (int(m.group(1)), mo)

    # Ano puro: 2025
    if re.fullmatch(r"\d{4}", s):
        return (int(s),)

    return None


def _compute_by_period(
    columns: list[str],
    rows: list[tuple],
    measure_idx: int,
    measure_indices: list[int],
    unit: str,
) -> dict | None:
    """Computa by_period: deltas temporais para serie temporal simples.

    v4.3: emite quando TODAS as condicoes:
    - EXATAMENTE 1 coluna temporal no resultado
    - ZERO dimensoes nao-temporais (serie temporal simples)
    - 2 a 40 periodos
    - Labels parseiaveis e ordenaveis cronologicamente
    - Nenhum NaN/Inf/None em qualquer celula
    - Nenhum periodo duplicado

    Formato por periodo (ordenado cronologicamente):
    {"period": <label cru>, "sum": <float cru>,
     "delta_abs": <float|ausente no 1o>, "delta_pct": <float|ausente>}

    delta_pct = (atual-anterior)/anterior*100, SO quando anterior > 0.
    INVARIANTE: fsum(sums de by_period) == sum_of_rows.
    """
    # 1. Encontra a UNICA coluna temporal
    temp_info = _find_temporal_column(columns, measure_indices)
    if not temp_info:
        return None  # 0 ou 2+ temporais: nao e serie simples

    temp_idx, temp_col = temp_info

    # 2. Verifica ZERO dimensoes nao-temporais (se houver, by_dimension ja cobre)
    n_dims = _count_dimension_columns(list(columns), measure_indices)
    if n_dims > 0:
        return None  # Com entidade, by_period seria ambiguo

    # 3. Coleta (label, valor) por linha, validando TODAS as celulas da linha
    #    (a promessa "NaN/Inf/None em qualquer celula" valia so
    #    p/ a medida — agora None ou numerico nao-finito em QUALQUER celula
    #    das linhas admitidas omite o bloco inteiro)
    period_data: list[tuple[object, float]] = []

    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) <= max(temp_idx, measure_idx):
            return None  # Linha mal formada -> omite tudo

        for cel in row:
            if cel is None:
                return None
            if isinstance(cel, (int, float)) and not isinstance(cel, bool) and not _is_finite(float(cel)):
                return None
            if isinstance(cel, Decimal) and not _is_finite(float(cel)):
                return None

        val = _extract_numeric(row[measure_idx])
        if val is None:
            return None

        period_data.append((row[temp_idx], val))

    # 4. Verifica quantidade de periodos (2 a 40)
    n_periods = len(period_data)
    if n_periods < 2 or n_periods > 40:
        return None

    # 5. Parseia labels, checa duplicata pela CHAVE CANONICA (
    #    "2025-01" e "2025/01" sao o MESMO periodo) e ordena cronologicamente.
    #    "period" preserva o valor cru (int de EXTRACT(YEAR)
    #    fica int; date/datetime vira ISO; resto so-string se preciso).
    parsed: list[tuple[tuple, object, float]] = []
    seen_keys: set[tuple] = set()
    for label_raw, val in period_data:
        if isinstance(label_raw, (_dt.datetime, _dt.date)):
            label_str = label_raw.isoformat(sep=" ") if isinstance(label_raw, _dt.datetime) else label_raw.isoformat()
            period_value: object = label_str
        elif isinstance(label_raw, (str, int, float)):
            label_str = str(label_raw)
            period_value = label_raw
        else:
            label_str = str(label_raw)
            period_value = label_str
        key = _try_parse_sortable_label(label_str)
        if key is None:
            return None  # Label nao integralmente reconhecido -> omite
        if key in seen_keys:
            return None  # mesmo periodo em grafias diferentes -> omite
        seen_keys.add(key)
        parsed.append((key, period_value, val))

    # Ordena pela chave parseada (cronologico)
    parsed.sort(key=lambda x: x[0])

    # 6. Constroi a lista de periodos com deltas. Delta OBRIGATORIO nao-finito
    #    (overflow) => bloco inteiro omitido (emitir sem campo
    #    obrigatorio viola o contrato "delta_abs sempre que houver anterior").
    periods: list[dict] = []
    prev_sum: float | None = None

    for _key, period_value, sum_val in parsed:
        period: dict = {
            "period": period_value,
            "sum": sum_val,  # Valor CRU (arredondar e papel da UI)
        }

        if prev_sum is not None:
            delta_abs = sum_val - prev_sum
            if not _is_finite(delta_abs):
                return None
            period["delta_abs"] = delta_abs

            if prev_sum > 0:
                delta_pct = ((sum_val - prev_sum) / prev_sum) * 100
                if not _is_finite(delta_pct):
                    return None
                period["delta_pct"] = delta_pct

        periods.append(period)
        prev_sum = sum_val

    return {
        "column": temp_col,
        "unit": unit,
        "periods": periods,
    }


# v4: REMOVED _find_metric — no fallback lookup.
# The engine trusts plano.summable computed by _compute_summable in semantics_engine.
