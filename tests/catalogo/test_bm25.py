# -*- coding: utf-8 -*-
"""Indice lexical BM25 (catalogo/retrieval.py): o que o ranking espera dele."""

import pathlib

from mcp_tiago_dados_abertos.catalogo import retrieval as R


def _catalogo():
    return {
        "carga-energia": {
            "name": "carga-energia", "tags": ["carga", "energia"], "vector_tags": ["demanda"],
            "rag_context": "carga de energia verificada por subsistema", "columns": [],
        },
        "geracao-usina": {
            "name": "geracao-usina", "tags": ["geracao", "usina"], "vector_tags": ["producao"],
            "rag_context": "geração verificada por usina", "columns": [],
        },
        "antigo": {"name": "antigo", "tags": ["carga"], "status": "discontinued", "columns": []},
    }


def test_indice_pontua_normalizado_e_ordenado():
    idx = R.SemanticIndex()
    idx.build(_catalogo())
    res = idx.search("carga de energia do subsistema", top_n=5)
    assert res and res[0][1] == "carga-energia"
    assert res[0][0] == 1.0, "o primeiro colocado vale 1.0; o ranking depende dessa escala"
    assert all(0.0 < s <= 1.0 for s, _, _ in res)
    assert [s for s, _, _ in res] == sorted((s for s, _, _ in res), reverse=True)


def test_acentos_nao_importam():
    idx = R.SemanticIndex()
    idx.build(_catalogo())
    com = idx.search("geração por usina", top_n=1)
    sem = idx.search("geracao por usina", top_n=1)
    assert com and sem and com[0][1] == sem[0][1] == "geracao-usina"


def test_descontinuado_fica_fora_e_sem_casamento_devolve_vazio():
    idx = R.SemanticIndex()
    idx.build(_catalogo())
    assert all(n != "antigo" for _, n, _ in idx.search("carga", top_n=10))
    assert idx.search("xyzzy inexistente", top_n=5) == []


def test_indice_reconstroi_quando_o_catalogo_muda():
    cat = _catalogo()
    R.build_index(cat)
    assert R._index.matches(cat)
    outro = {k: v for k, v in cat.items() if k != "geracao-usina"}
    assert not R._index.matches(outro)
    R.ensure_semantic_index(outro)
    assert R._index.matches(outro)


def test_busca_nao_depende_de_scikit_learn():
    """A troca do TF-IDF pelo BM25 tirou scikit-learn, scipy e numpy da imagem; nenhum modulo
    do pacote pode reintroduzir o import sem que este teste avise."""
    src = pathlib.Path(__file__).resolve().parents[2] / "src"
    culpados = [p for p in src.rglob("*.py") if "sklearn" in p.read_text(encoding="utf-8")]
    assert not culpados, [str(p) for p in culpados]
