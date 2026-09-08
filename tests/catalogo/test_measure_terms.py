# -*- coding: utf-8 -*-
"""Vocabulario de medida (semantics.measure_terms) no ranking.

Dois datasets do mesmo assunto e ativo, medidas diferentes: a pergunta que cita a medida
faz o contrato certo ganhar uma vantagem fixa, contada uma vez por contrato; sem citar a
medida, nada muda. Catalogo sintetico com orgao ficticio: o comportamento vem do contrato.
"""

import copy

from mcp_tiago_dados_abertos.catalogo import ranking as K
from mcp_tiago_dados_abertos.catalogo import retrieval as R
from mcp_tiago_dados_abertos.catalogo.contracts import rank_datasets


def _catalogo():
    def ds(name, termos):
        return {
            "orgao": "xx", "name": name, "status": "active", "granularity": "horária",
            "tags": ["usina", "energia da usina", "producao da usina"], "vector_tags": [],
            "rag_context": "dados por usina em base horaria", "columns": [],
            "semantics": {"measure_terms": termos, "row_grain": {"temporal": "1 hora", "spatial": "usina"}},
        }
    return {
        "xx/producao": ds("producao", ["producao", "produziu", "gerou"]),
        "xx/corte": ds("corte", ["corte", "cortes", "curtailment", "constrained off"]),
    }


def _escores(cat, pergunta):
    R.build_index(cat)
    return {n: s for s, n, _ in rank_datasets(cat, pergunta, 3)}


def _sem_campo(cat):
    cat = copy.deepcopy(cat)
    for m in cat.values():
        m["semantics"].pop("measure_terms")
    return cat


def test_medida_citada_da_vantagem_exata_so_ao_contrato_da_medida():
    cat = _catalogo()
    for pergunta, esperado in (("corte de energia da usina x", "corte"), ("quanto a usina x produziu", "producao"),
                               ("constrained off da usina x", "corte")):
        com, sem = _escores(cat, pergunta), _escores(_sem_campo(cat), pergunta)
        outro = "producao" if esperado == "corte" else "corte"
        assert com[esperado] - sem[esperado] == K._MEDIDA_PESO, pergunta
        assert com[outro] == sem[outro], pergunta


def test_lista_longa_nao_pesa_mais():
    cat = _catalogo()
    cat["xx/corte"]["semantics"]["measure_terms"] += ["corte de geracao", "cortes de geracao", "geracao cortada"]
    com, sem = _escores(cat, "cortes de geracao da usina x"), _escores(_sem_campo(cat), "cortes de geracao da usina x")
    assert com["corte"] - sem["corte"] == K._MEDIDA_PESO


def test_sem_medida_na_pergunta_nada_muda():
    cat = _catalogo()
    assert _escores(cat, "dados da usina x em 2024") == _escores(_sem_campo(cat), "dados da usina x em 2024")
