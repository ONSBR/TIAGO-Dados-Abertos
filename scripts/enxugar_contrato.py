# -*- coding: utf-8 -*-
"""Enxuga contratos ODCS: remove o que NENHUM caminho do servidor le nem mostra.

Motivacao: ~34% do corpus era
campo que o motor nao consulta E que nenhuma tool coloca na resposta. A maior
parte nem era metadado de dados — era rastro de COMO o contrato foi gerado
(`relatedDatasets[*].evidencias`, `.raciocinio`, `.llm_confidence`, `.score`).

Regra do corte, e por que ela e' segura:
  - So sai caminho FIXO e conhecido (nunca chave de dado: nome de dataset em
    relatedDatasets, nome de coluna em value_aliases). Um teste de nome generico
    cortaria `capacidade-geracao` e `nom_bacia`, que sao conteudo.
  - O script se recusa a cortar chave que apareca como literal em src/: se
    alguem passar a ler o campo, o corte falha alto em vez de mutilar calado.
  - O aceite e' o render: `descrever_dataset` dos datasets antes e depois tem
    que dar diff VAZIO. Se o que o modelo ve nao muda, nada vivo foi tocado.

Uso:
  python scripts/enxugar_contrato.py --check    # lista o que sairia, nao grava
  python scripts/enxugar_contrato.py            # aplica
  python scripts/enxugar_contrato.py --portal ons
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml

RAIZ = Path(__file__).resolve().parents[1]

# ── O que sai ─────────────────────────────────────────────────────────────────

# customProperties do contrato, campo inteiro
CP_CONTRATO = {
    "diagnosticQueries",   # SQL de diagnostico: ninguem executa, ninguem mostra
    "coberturaDerivadaEm",  # carimbo de quando a cobertura foi derivada
    "derivedCoverageFrom",  # o mesmo campo, na convencao em ingles
    "total_arquivos_s3",    # contagem de arquivos no momento da geracao
    "s3FileCount",          # idem, na convencao em ingles
    "totalRows",
    "aiProvenance",         # rastro do pipeline que gerou o contrato
    "csvResources",
    "evidenceRegistry",
    "availableResources",
    "organizacao",
}

# chaves dentro de customProperties.semantics
SEMANTICS = {
    "safe_joins",
    "unsupported_concepts",
    "provenance",
    "validation",
    "hard_negatives",
    "hard_negatives_note",
    "availability",
    "numeric_attributes",
    "listing_columns",
    "semantics_schema_version",
    "data_quality_warnings",
    "data_quality_notes",
    "data_coverage_note",
    "aggregation_warnings",
    "sql_snippets",
    "runtime_source",
    "data_nature",
}

# customProperties de COLUNA
CP_COLUNA = {
    "evidenceLevel",
    "sourceOfTruth",
    "governanceScore",
    "nullPercentage",
    "castRequired",      # a flag; quem manda e' castExpression
    "enumDescriptions",
}

# chaves diretas de coluna
COLUNA = {"primaryKeyPosition"}

# Sub-chaves de semantics.metrics[]: a familia de "notas" que o motor nunca leu.
# ATENCAO: varias delas sao conhecimento CURADO que o motor ainda nao implementa
# (sentinel_values traz o -9999.9 do equipamento-controle-reativo com o
# filter_expression pronto; safe_filter e null_handling idem). Saem daqui porque o
# repositorio nao carrega campo que nenhum caminho le.
METRICA = {
    "notes", "data_quality_note", "data_quality_notes", "data_warnings",
    "null_note", "null_warning", "null_handling", "null_rate", "null_fraction",
    "type_change_note", "cast_warning", "cast_note", "cast_required",
    "display_note", "outlier_note", "range_note", "usage_note",
    "sentinel_values", "safe_filter", "unit_inferred_from_name",
}

# semantics.query_routing: as flags supports_* SAO renderizadas (o descrever_dataset
# as monta por PREFIXO, nao por nome), entao so estas duas saem.
QUERY_ROUTING = {"drill_down_policy", "scan_cost"}

SELECTION_POLICY = {"covers_dimensions"}

# semantics.sql_patterns[*] e ambiguity_policy
# carimbos de validacao: o servidor nao os le mais; validated_params fica (dado do motor)
SQL_PATTERN = {"repaired_sql", "notes", "repaired", "validated", "rows_returned",
               "validation_error", "validation_params"}
AMBIGUITY = {"known_metrics", "data_quality_warnings"}

# team FICA inteiro, inclusive members: a caixa institucional do portal e' o canal
# de quem acha problema no DADO (que e' do ONS, nao nosso). Nao e' sobra, e'
# atribuicao — mesmo criterio de `description` e `license`.
TEAM: set[str] = set()

# Poda POR DENTRO: destes dois, so o que e' lido/renderizado sobrevive.
RELATED_MANTER = {"join_keys"}
FEWSHOT_MANTER = {"question", "sql", "code", "validated_params", "template"}
# Tudo que NAO esta em FEWSHOT_MANTER sai — inclusive `validation_note`
# ("Corrigido TRY_CAST em ..."), que e' rastro de quem gerou o conserto.


def _literais_do_src() -> str:
    src = RAIZ / "src"
    return " \n".join(p.read_text(encoding="utf-8", errors="ignore") for p in src.rglob("*.py"))


def _guarda(fonte: str) -> None:
    """Nao cortar campo que alguem passou a ler."""
    vivos = []
    for chave in (CP_CONTRATO | SEMANTICS | CP_COLUNA | COLUNA | METRICA
                  | QUERY_ROUTING | SELECTION_POLICY | SQL_PATTERN | AMBIGUITY | TEAM):
        if re.search(r"""["']%s["']""" % re.escape(chave), fonte):
            vivos.append(chave)
    if vivos:
        sys.exit(
            "ABORTADO: estes campos aparecem em src/ e nao podem ser cortados: "
            + ", ".join(sorted(vivos))
        )


def enxuga(doc: dict) -> int:
    """Remove os campos do contrato in-place. Retorna quantos saíram."""
    n = 0
    cps = doc.get("customProperties")
    if isinstance(cps, list):
        antes = len(cps)
        cps[:] = [c for c in cps if not (isinstance(c, dict) and c.get("property") in CP_CONTRATO)]
        n += antes - len(cps)
        for c in cps:
            if not isinstance(c, dict):
                continue
            prop, val = c.get("property"), c.get("value")
            if prop == "semantics" and isinstance(val, dict):
                alvo = val.get("semantics") if isinstance(val.get("semantics"), dict) else val
                for k in list(alvo):
                    if k in SEMANTICS:
                        del alvo[k]
                        n += 1
                for m in alvo.get("metrics") or []:
                    if isinstance(m, dict):
                        for k in list(m):
                            if k in METRICA:
                                del m[k]
                                n += 1
                for bloco, fora in (("query_routing", QUERY_ROUTING),
                                    ("selection_policy", SELECTION_POLICY),
                                    ("ambiguity_policy", AMBIGUITY)):
                    b = alvo.get(bloco)
                    if isinstance(b, dict):
                        for k in list(b):
                            if k in fora:
                                del b[k]
                                n += 1
                pats = alvo.get("sql_patterns")
                if isinstance(pats, dict):
                    for _, pat in pats.items():
                        if isinstance(pat, dict):
                            for k in list(pat):
                                if k in SQL_PATTERN:
                                    del pat[k]
                                    n += 1
            elif prop == "relatedDatasets" and isinstance(val, dict):
                for _, rel in val.items():
                    if isinstance(rel, dict):
                        for k in list(rel):
                            if k not in RELATED_MANTER:
                                del rel[k]
                                n += 1
            elif prop == "fewShotQueries" and isinstance(val, list):
                for fq in val:
                    if isinstance(fq, dict):
                        for k in list(fq):
                            if k not in FEWSHOT_MANTER:
                                del fq[k]
                                n += 1

    for sch in doc.get("schema") or []:
        for prop in sch.get("properties") or []:
            for k in list(prop):
                if k in COLUNA:
                    del prop[k]
                    n += 1
            ccp = prop.get("customProperties")
            if isinstance(ccp, list):
                antes = len(ccp)
                ccp[:] = [c for c in ccp if not (isinstance(c, dict) and c.get("property") in CP_COLUNA)]
                n += antes - len(ccp)
                if not ccp:
                    del prop["customProperties"]
    return n


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="lista o que sairia, sem gravar")
    ap.add_argument("--portal", default="ons")
    args = ap.parse_args()

    _guarda(_literais_do_src())

    base = RAIZ / "contracts" / args.portal
    antes = depois = removidos = 0
    for f in sorted(base.glob("*.odcs.yaml")):
        bruto = f.read_text(encoding="utf-8")
        antes += len(bruto)
        doc = yaml.safe_load(bruto)
        removidos += enxuga(doc)
        novo = yaml.safe_dump(doc, allow_unicode=True, sort_keys=False, width=100)
        depois += len(novo)
        if not args.check:
            f.write_text(novo, encoding="utf-8")
    verbo = "sairiam" if args.check else "sairam"
    print(f"{antes/1024:.0f} KB -> {depois/1024:.0f} KB  ({(antes-depois)*100/antes:.1f}% menor)")
    print(f"{removidos} campos {verbo} de {len(list(base.glob('*.odcs.yaml')))} contratos")


if __name__ == "__main__":
    main()
