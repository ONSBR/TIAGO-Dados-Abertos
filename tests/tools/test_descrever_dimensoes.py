# -*- coding: utf-8 -*-
"""Dimensao que nao e' coluna nao pode ser anunciada como filtravel.

semantics.value_aliases lista CONCEITOS, e nem todo conceito e' coluna. Em quatro
contratos do corpus 'fonte' e' a leitura conjunta de varias colunas de geracao, nao
uma coluna. Anunciar como filtravel leva o modelo a escrever WHERE fonte = 'eolica'
e receber Binder Error.

Quem sabe a diferenca e' semantics.dimension_columns, que marca o caso com _pivot(...).
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


def test_pivot_nao_aparece_como_filtravel(catalog):
    """No balanco por subsistema, 'fonte' e' pivo de quatro colunas de geracao."""
    out = _descreve("balanco-energia-subsistema", catalog)
    linha = next((x for x in out.splitlines() if "Dimensoes filtraveis" in x), "")
    assert linha, "a linha de dimensoes filtraveis sumiu"
    assert "fonte" not in linha, f"'fonte' nao e' coluna e foi anunciada: {linha}"
    assert "subsistema" in linha, "a dimensao real precisa continuar listada"


def test_pivot_ganha_aviso_explicito(catalog):
    out = _descreve("balanco-energia-subsistema", catalog)
    assert "`fonte` NAO e' coluna" in out
    # o aviso precisa dizer QUAIS colunas formam o pivo, senao nao ajuda
    assert "val_gereolica" in out and "val_gerhidraulica" in out


def test_dimensao_real_continua_filtravel(catalog):
    """Contrato sem pivo nao pode perder nada."""
    out = _descreve("carga-energia", catalog)
    linha = next((x for x in out.splitlines() if "Dimensoes filtraveis" in x), "")
    assert "subsistema" in linha
    assert "NAO e' coluna" not in out


def test_todo_pivo_do_corpus_esta_avisado(catalog):
    """Guarda contra contrato novo que introduza pivo sem o aviso."""
    com_pivo = set()
    for meta in catalog.values():
        sem = meta.get("semantics") or {}
        for k, v in (sem.get("dimension_columns") or {}).items():
            if isinstance(v, str) and v.startswith("_pivot(") and k in (sem.get("value_aliases") or {}):
                com_pivo.add(meta.get("name"))
    assert com_pivo, "nenhum pivo no corpus: o teste perdeu o objeto"
    for nome in sorted(com_pivo):
        assert "NAO e' coluna" in _descreve(nome, catalog), f"{nome} tem pivo sem aviso"
