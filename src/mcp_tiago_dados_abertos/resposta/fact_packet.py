# -*- coding: utf-8 -*-
"""fact_packet — FATOS TIPADOS a partir dos computed-facts + result_schema.

Cada fato: {id, ref, kind, column, label, metric, value, unit, dims, query_id, tier}.
  kind: total | mean | value | share | rank | delta_abs | delta_pct
  id:   deterministico e ASCII — "<kind>:<coluna>[:<slug do rotulo>]"; colisao de slug
        recebe sufixo de hash do rotulo e e reportada (nunca descarte silencioso).
  ref:  referencia CURTA sequencial (f1, f2, ...) que o modelo copia como {f1} — ele nunca
        precisa montar o id.
  unit: do result_schema (coluna do resultado) ou do proprio computed-facts; share e
        delta_pct sao '%'; COUNT nao tem unidade fisica.
Pacote completo = {facts, abstentions[{kind, column, reason_code}], coverage{complete}}:
o que NAO foi emitido e dito com motivo — e o orquestrador decide reconsultar.
Legado (sum_of_rows/by_dimension/by_period) nao nomeia a coluna: so e tipado quando o
resultado tem EXATAMENTE 1 medida; 2+ => abstencao ambiguous_measure.
Nada e recalculado aqui — so re-tipado. Nunca lanca.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata

_NAO_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def slug(texto) -> str:
    s = unicodedata.normalize("NFKD", str(texto if texto is not None else "")).encode("ascii", "ignore").decode()
    s = _NAO_ALNUM_RE.sub("_", s.lower()).strip("_")
    return s[:40] or "_"


def _humano(coluna: str) -> str:
    return re.sub(r"[_\s]+", " ", str(coluna or "")).strip()


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


class _Pacote:
    def __init__(self, schema, motivos=None):
        self.motivos = motivos if isinstance(motivos, dict) else {}
        schema = schema if isinstance(schema, dict) else {}
        self.qid = schema.get("query_id")
        self.tier = ((schema.get("route") or {}).get("tier")) or "adhoc"
        self.cols = {str(c.get("name")): c for c in (schema.get("columns") or []) if isinstance(c, dict)}
        self.out: list[dict] = []
        self.ids: set[str] = set()
        self.abst: list[dict] = []

    def col(self, nome):
        return self.cols.get(str(nome)) or {}

    def unit(self, nome, fallback=None):
        c = self.col(nome)
        return c.get("unit") if c.get("unit") is not None else fallback

    def aditiva(self, nome, fallback=True):
        c = self.col(nome)
        return bool(c.get("additive")) if c else fallback

    def abstem(self, kind, coluna, reason):
        self.abst.append({"kind": kind, "column": str(coluna), "reason_code": reason})

    def add(self, kind, coluna, value, unit, dims, sufixo=None):
        value = _num(value)
        if value is None:
            return
        fid = f"{kind}:{slug(coluna)}" + (f":{slug(sufixo)}" if sufixo is not None else "")
        if fid in self.ids:
            # colisao de slug (rotulos distintos com o mesmo slug): id derivado do rotulo
            # completo — e reportado; jamais descarta o segundo grupo
            fid = fid + "_" + hashlib.sha1(str(sufixo).encode("utf-8"), usedforsecurity=False).hexdigest()[:6]
            self.abstem(kind, coluna, "id_collision")
            if fid in self.ids:
                return
        self.ids.add(fid)
        self.out.append({
            "id": fid, "ref": f"f{len(self.out) + 1}", "kind": kind, "column": str(coluna),
            "label": _humano(coluna), "metric": self.col(coluna).get("metric"), "value": value,
            "unit": unit, "dims": dict(dims or {}), "query_id": self.qid, "tier": self.tier,
        })


def _by_rows(p: _Pacote, br: dict):
    dims = [str(x) for x in (br.get("dimension") or [])] or ["linha"]
    measures = br.get("measures") or {}
    if not isinstance(measures, dict):
        return

    def _dims(label) -> dict:
        # by_rows junta as colunas de dimensao com " | " (codigo | nome): re-separa
        partes = [s.strip() for s in str(label if label is not None else "").split(" | ")]
        if len(partes) == len(dims):
            return dict(zip(dims, partes))
        return {dims[0]: label}

    for coluna, m in measures.items():
        if not isinstance(m, dict):
            continue
        unit = p.unit(coluna)
        groups = [g for g in (m.get("groups") or []) if isinstance(g, dict)]
        tem_pct = any("pct" in g for g in groups)
        if tem_pct and p.aditiva(coluna):
            p.add("total", coluna, m.get("total"), unit, {})
        else:
            # motivo REAL do by_rows quando existe (valores_negativos, total_nao_positivo...);
            # sem motivo, a coluna nao e aditiva
            motivo = str((p.motivos or {}).get(coluna) or "non_additive")
            if motivo == "nao_aditiva_sem_pct":
                motivo = "non_additive"
            p.abstem("total", coluna, motivo)
            p.abstem("share", coluna, motivo)
        for g in groups:
            label = g.get("label")
            d = _dims(label)
            p.add("value", coluna, g.get("value"), unit, d, label)
            p.add("rank", coluna, g.get("rank"), None, d, label)
            if "pct" in g:
                p.add("share", coluna, g.get("pct"), "%", d, label)


def _medida_unica(p: _Pacote):
    """Coluna 'measure' do schema quando ha EXATAMENTE uma; None (ambiguo/sem schema) caso contrario."""
    medidas = [nome for nome, c in p.cols.items() if c.get("role") == "measure"]
    return medidas[0] if len(medidas) == 1 else None


def _legado(p: _Pacote, facts: dict):
    kinds = {"sum_of_rows": "total", "mean_of_rows": "mean", "by_dimension": "share", "by_period": "delta_pct"}
    presentes = [k for k in kinds if isinstance(facts.get(k), dict)]
    if not presentes:
        return
    col = _medida_unica(p)
    if not col:
        n = len([1 for c in p.cols.values() if c.get("role") == "measure"])
        for k in presentes:
            p.abstem(kinds[k], "?", "ambiguous_measure" if n >= 2 else "no_measure")
        return
    sor = facts.get("sum_of_rows")
    if isinstance(sor, dict):
        p.add("total", col, sor.get("value"), p.unit(col, sor.get("unit")), {})
    mor = facts.get("mean_of_rows")
    if isinstance(mor, dict):
        p.add("mean", col, mor.get("value"), p.unit(col, mor.get("unit")), {})
    bd = facts.get("by_dimension")
    if isinstance(bd, dict):
        dim = str(bd.get("column") or "dimensao")
        unit = p.unit(col, bd.get("unit"))
        for g in bd.get("groups") or []:
            if not isinstance(g, dict):
                continue
            key = g.get("key")
            d = {dim: key}
            p.add("value", col, g.get("sum"), unit, d, key)
            p.add("rank", col, g.get("rank"), None, d, key)
            if "pct" in g:
                p.add("share", col, g.get("pct"), "%", d, key)
    bp = facts.get("by_period")
    if isinstance(bp, dict):
        tcol = str(bp.get("column") or "periodo")
        unit = p.unit(col, bp.get("unit"))
        for per in bp.get("periods") or []:
            if not isinstance(per, dict):
                continue
            key = per.get("period")
            d = {tcol: key}
            p.add("value", col, per.get("sum"), unit, d, key)
            if "delta_abs" in per:
                p.add("delta_abs", col, per.get("delta_abs"), unit, d, key)
            if "delta_pct" in per:
                p.add("delta_pct", col, per.get("delta_pct"), "%", d, key)


def fact_packet_completo(facts, schema, *, truncated: bool = False, motivos=None) -> dict:
    """{facts, abstentions, coverage}. Nunca lanca."""
    vazio = {"facts": [], "abstentions": [], "coverage": {"complete": not truncated}}
    try:
        if not isinstance(facts, dict):
            return vazio
        p = _Pacote(schema, motivos)
        br = facts.get("by_rows")
        if isinstance(br, dict):
            _by_rows(p, br)
        # serie temporal pura: so o TOTAL do periodo por medida aditiva (sem share entre meses)
        for coluna, valor in (facts.get("totals") or {}).items():
            p.add("total", coluna, valor, p.unit(coluna), {})
        _legado(p, facts)
        cobertas = {a["column"] for a in p.abst}
        for col, motivo in (motivos or {}).items():
            if col == "_":
                p.abstem("share", "*", str(motivo))
            elif col not in cobertas:  # medida que nem entrou no by_rows (celula nao numerica, etc.)
                p.abstem("share", col, str(motivo))
        return {"facts": p.out, "abstentions": p.abst, "coverage": {"complete": not truncated}}
    except Exception:
        return vazio


def fact_packet(facts, schema) -> list[dict]:
    """Lista de fatos tipados (compat). Nunca lanca."""
    return fact_packet_completo(facts, schema)["facts"]
