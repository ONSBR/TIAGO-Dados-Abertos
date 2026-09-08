# -*- coding: utf-8 -*-
"""Coluna que so' existe em parte da serie tem de avisar ANTES do SQL.

A fonte publica acrescenta coluna no meio da serie. Como todo parquet_source usa
union_by_name=true, o periodo anterior volta NULL e nao erro, e AVG/SUM ignoram
nulo: a media da serie sai da media do trecho que tem a coluna. Medido em
dados-hidrologicos-res, onde a coluna cobre 4 de 27 anos.

O modelo le descrever_dataset antes de escrever SQL, entao e' la' que o aviso
precisa estar. Estes testes garantem que ele chega, e que ninguem o remove sem
perceber.
"""

import asyncio

import pytest

from mcp_tiago_dados_abertos.catalogo.contracts import load_all_contracts
from mcp_tiago_dados_abertos.tools.descrever_dataset import descrever_dataset


@pytest.fixture(scope="module")
def catalog():
    return load_all_contracts()


def _descreve(nome, catalog):
    return asyncio.run(descrever_dataset(nome, catalog=catalog))


def _colunas_parciais(catalog):
    """Colunas que declaram availableFrom, direto do catalogo carregado."""
    fora = {}
    for meta in catalog.values():
        cols = [c["name"] for c in (meta.get("columns") or []) if c.get("available_from")]
        if cols:
            fora[meta.get("name")] = cols
    return fora


def test_corpus_tem_coluna_parcial_declarada(catalog):
    """Se este teste falhar, ou o corpus mudou ou a carga parou de ler o campo."""
    assert _colunas_parciais(catalog), "nenhuma coluna com availableFrom foi carregada"


def test_aviso_chega_ao_descrever(catalog):
    for nome, cols in _colunas_parciais(catalog).items():
        out = _descreve(nome, catalog)
        assert "ATENCAO_disponivel_a_partir_de" in out, (
            f"{nome} tem coluna parcial ({cols}) e o descrever_dataset nao avisa")


def test_aviso_explica_o_risco(catalog):
    """Dizer 'a partir de 2023' sem dizer o efeito nao muda o comportamento."""
    nome = next(iter(_colunas_parciais(catalog)))
    out = _descreve(nome, catalog)
    assert "ATENCAO_cobertura" in out
    nota = next(x for x in out.splitlines() if "ATENCAO_cobertura" in x)
    assert "NULL" in nota, "o aviso precisa dizer que vem NULL, nao erro"
    assert "ignoram nulo" in nota or "AVG" in nota, "precisa dizer o efeito na agregacao"


def test_dados_hidrologicos_e_o_caso_medido(catalog):
    """Caso concreto: a coluna cobre 4 de 27 periodos."""
    out = _descreve("dados-hidrologicos-res", catalog)
    assert "val_vazaoincrementalbruta" in out
    nota = [x for x in out.splitlines() if "ATENCAO_cobertura" in x]
    assert nota and "2023" in nota[0]


def test_coluna_completa_nao_ganha_aviso(catalog):
    """Ruido tira a forca do aviso: coluna que cobre tudo fica quieta."""
    out = _descreve("carga-energia", catalog)
    assert "ATENCAO_disponivel_a_partir_de" not in out
