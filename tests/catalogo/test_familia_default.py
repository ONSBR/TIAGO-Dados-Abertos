# -*- coding: utf-8 -*-
"""Dataset de referencia da familia (semantics.family / family_default) no ranking.

Irmaos que so diferem no grao disputam a pergunta que nao diz o grao. O contrato marcado
como referencia da familia recebe uma vantagem fixa nessa disputa; a pergunta com grao
explicito nao e' tocada. Catalogo sintetico com orgao ficticio: prova que o comportamento
vem do contrato lido, nao de uma constante no codigo.
"""

import copy

from mcp_tiago_dados_abertos.catalogo import ranking as K
from mcp_tiago_dados_abertos.catalogo import retrieval as R
from mcp_tiago_dados_abertos.catalogo.contracts import rank_datasets


def _catalogo(default_em: str):
    def ds(name, gran, temporal, default):
        return {
            "orgao": "xx", "name": name, "status": "active", "granularity": gran,
            "tags": ["consumo", "consumo de energia", "consumo por regiao"], "vector_tags": [],
            "rag_context": "consumo de energia por regiao", "columns": [],
            "semantics": {"family": "consumo", "family_default": default,
                          "row_grain": {"temporal": temporal, "spatial": "regiao"}},
        }
    return {
        "xx/consumo-diario": ds("consumo-diario", "diária", "1 dia", default_em == "consumo-diario"),
        "xx/consumo-horario": ds("consumo-horario", "horária", "1 hora", default_em == "consumo-horario"),
    }


def _escores(cat, pergunta):
    R.build_index(cat)
    return {n: s for s, n, _ in rank_datasets(cat, pergunta, 3)}


def _sem_campos(cat):
    cat = copy.deepcopy(cat)
    for m in cat.values():
        m["semantics"].pop("family")
        m["semantics"].pop("family_default")
    return cat


def test_sem_grao_na_pergunta_a_referencia_vence():
    pergunta = "consumo de energia da regiao sul em 2024"
    assert max(_escores(_catalogo("consumo-diario"), pergunta).items(), key=lambda x: x[1])[0] == "consumo-diario"
    assert max(_escores(_catalogo("consumo-horario"), pergunta).items(), key=lambda x: x[1])[0] == "consumo-horario"


def test_sem_grao_a_vantagem_e_exatamente_o_peso_e_so_para_a_referencia():
    cat = _catalogo("consumo-diario")
    pergunta = "consumo de energia da regiao sul em 2024"
    com, sem = _escores(cat, pergunta), _escores(_sem_campos(cat), pergunta)
    assert com["consumo-diario"] - sem["consumo-diario"] == K._FAMILIA_PESO
    assert com["consumo-horario"] == sem["consumo-horario"]


def test_com_grao_na_pergunta_os_campos_nao_mudam_nada():
    cat = _catalogo("consumo-diario")
    for pergunta in ("consumo de energia por hora na regiao sul", "consumo mensal da regiao sul", "consumo de ontem"):
        assert _escores(cat, pergunta) == _escores(_sem_campos(cat), pergunta), pergunta
