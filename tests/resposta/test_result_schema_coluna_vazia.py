# -*- coding: utf-8 -*-
"""Coluna que volta inteiramente nula ganha aviso no result-schema.

A fonte publica acrescenta e renomeia colunas ao longo da serie. Como todo
parquet_source usa union_by_name=true, o arquivo que nao tem a coluna devolve NULL
em vez de erro, e AVG/SUM ignoram nulo -- o numero sai plausivel e errado. O aviso
existe para o modelo nao apresentar esse numero como fato.

A checagem olha o dado devolvido, nao metadado, entao pega tambem o drift que
nenhuma auditoria levantou ainda.
"""

from mcp_tiago_dados_abertos.resposta.result_schema import result_schema

SQL = "SELECT ano, val_vazaoincrementalbruta FROM read_parquet('s3://b/x/*.parquet')"


def _cols(rows, colunas=("ano", "val_vazaoincrementalbruta")):
    rs = result_schema(SQL, list(colunas), [], rows=rows)
    return {c["name"]: c for c in rs["columns"]}


def test_coluna_toda_nula_e_marcada():
    c = _cols([("2021", None), ("2022", None), ("2023", None)])
    assert c["val_vazaoincrementalbruta"].get("all_null") is True
    assert "descrever_dataset" in c["val_vazaoincrementalbruta"]["note"]
    # a coluna preenchida ao lado nao e' marcada
    assert "all_null" not in c["ano"]


def test_coluna_parcialmente_nula_nao_e_marcada():
    """Nulo parcial e comum em dado real; marcar viraria ruido."""
    c = _cols([("2022", None), ("2023", 361.06), ("2024", 279.19)])
    assert "all_null" not in c["val_vazaoincrementalbruta"]


def test_resultado_sem_linhas_nao_marca_nada():
    """Sem linhas nao ha evidencia de nada: zero linhas nao prova coluna vazia."""
    for rows in ([], None):
        c = _cols(rows)
        assert "all_null" not in c["val_vazaoincrementalbruta"]
        assert "all_null" not in c["ano"]


def test_linha_curta_nao_conta_como_vazia():
    """Linha mais curta que o cabecalho e' dado malformado, nao coluna vazia."""
    c = _cols([("2023",), ("2024", 279.19)])
    assert "all_null" not in c["val_vazaoincrementalbruta"]


def test_todas_as_colunas_nulas():
    c = _cols([(None, None), (None, None)])
    assert c["ano"].get("all_null") is True
    assert c["val_vazaoincrementalbruta"].get("all_null") is True
