# -*- coding: utf-8 -*-
"""Poda de particao: a propriedade que importa e' NAO PODAR quando ha duvida.

prune_user_sql reescreve o SQL do usuario trocando o glob de todos os anos por uma
lista de arquivos. Se ele podar sem prova, a consulta le menos arquivos do que
deveria e devolve numero MENOR sem nenhum erro — falha silenciosa, a pior classe.

Por isso a maioria destes testes verifica o fallback: entrada duvidosa tem de sair
identica. Os poucos que verificam a poda em si conferem que os anos escolhidos
cobrem o que o WHERE pede.

A listagem no S3 e' substituida por um duplo: nenhum teste sai para a rede.
"""

import pytest

from mcp_tiago_dados_abertos.execucao import pruning

GLOB = "read_parquet('s3://ons-aws-prod-opendata/dataset/carga_energia_di/*.parquet')"
CATALOGO = {
    "ons/carga-energia": {
        "name": "carga-energia",
        "parquet_source": GLOB,
        "prune_partition": "year",
        "semantics": {"row_grain": {"temporal_column": "din_instante"}},
    },
    # mesmo dataset, mas SEM a flag: o contrato reprovou na validacao de poda
    "ons/sem-flag": {
        "name": "sem-flag",
        "parquet_source": "read_parquet('s3://b/dataset/sem_flag_di/*.parquet')",
        "prune_partition": "",
        "semantics": {"row_grain": {"temporal_column": "din_instante"}},
    },
}


@pytest.fixture(autouse=True)
def sem_rede(monkeypatch):
    """Substitui a listagem do S3: os arquivos existem, de 2000 a 2026."""
    def _fake(bucket, prefix):
        return [f"{prefix}CARGA_ENERGIA_{a}.parquet" for a in range(2000, 2027)]
    monkeypatch.setattr(pruning, "_list", _fake)


def _sel(where):
    return f"SELECT * FROM {GLOB} WHERE {where}"


def _arquivos(sql):
    """So' os nomes de arquivo dentro do read_parquet.

    Procurar o ano no SQL inteiro daria falso positivo: '2026-01-01' do proprio
    WHERE casaria com '2026'.
    """
    import re
    m = re.search(r"read_parquet\((.*?)\)\s", sql + " ", re.S)
    return m.group(1) if m else ""


# --- o que DEVE podar -------------------------------------------------------

def test_range_fechado_poda():
    sql = _sel("din_instante >= '2025-01-01' AND din_instante < '2026-01-01'")
    out = pruning.prune_user_sql(sql, CATALOGO)
    assert out != sql, "range fechado prova o ano e deveria podar"
    arqs = _arquivos(out)
    assert "2025" in arqs
    assert "2024" not in arqs and "2026" not in arqs


def test_extract_year_pontual_poda():
    sql = _sel("EXTRACT(YEAR FROM TRY_CAST(din_instante AS DATE)) = 2023")
    out = pruning.prune_user_sql(sql, CATALOGO)
    assert out != sql and "2023" in _arquivos(out)


# --- o que NAO pode podar (fallback seguro) ---------------------------------

def test_range_aberto_nao_poda():
    """So' piso: linhas de qualquer ano posterior qualificam."""
    sql = _sel("din_instante >= '2025-01-01'")
    assert pruning.prune_user_sql(sql, CATALOGO) == sql


def test_sem_where_nao_poda():
    sql = f"SELECT count(*) FROM {GLOB}"
    assert pruning.prune_user_sql(sql, CATALOGO) == sql


def test_dois_globs_nao_poda():
    """JOIN ou subquery: a prova de um glob nao vale para o outro."""
    sql = (f"SELECT * FROM {GLOB} a JOIN {GLOB} b ON a.x = b.x "
           "WHERE a.din_instante >= '2025-01-01' AND a.din_instante < '2026-01-01'")
    assert pruning.prune_user_sql(sql, CATALOGO) == sql


def test_dataset_sem_flag_nao_poda():
    """Contrato sem partitionPruning=year foi reprovado na validacao; respeitar."""
    sql = ("SELECT * FROM read_parquet('s3://b/dataset/sem_flag_di/*.parquet') "
           "WHERE din_instante >= '2025-01-01' AND din_instante < '2026-01-01'")
    assert pruning.prune_user_sql(sql, CATALOGO) == sql


def test_dataset_desconhecido_nao_poda():
    sql = ("SELECT * FROM read_parquet('s3://b/dataset/nao_existe_di/*.parquet') "
           "WHERE din_instante >= '2025-01-01' AND din_instante < '2026-01-01'")
    assert pruning.prune_user_sql(sql, CATALOGO) == sql


def test_ano_em_coluna_qualquer_nao_poda():
    """2025 aparece, mas numa coluna que nao e' a temporal: nao prova nada."""
    sql = _sel("val_carga = 2025")
    assert pruning.prune_user_sql(sql, CATALOGO) == sql


def test_or_no_where_nao_poda():
    """A prova so' vale por conjunctos AND; OR abre outros anos."""
    sql = _sel("(din_instante >= '2025-01-01' AND din_instante < '2026-01-01') "
               "OR val_carga > 100")
    assert pruning.prune_user_sql(sql, CATALOGO) == sql


def test_sql_invalido_nao_quebra():
    sql = "SELECT * FROM " + GLOB + " WHERE (((("
    assert pruning.prune_user_sql(sql, CATALOGO) == sql


def test_sql_vazio_e_none():
    assert pruning.prune_user_sql("", CATALOGO) == ""
    assert pruning.prune_user_sql(None, CATALOGO) is None


def test_falha_ao_listar_nao_poda(monkeypatch):
    """S3 indisponivel nao pode virar consulta com menos arquivos."""
    def _explode(bucket, prefix):
        raise RuntimeError("S3 fora")
    monkeypatch.setattr(pruning, "_list", _explode)
    sql = _sel("din_instante >= '2025-01-01' AND din_instante < '2026-01-01'")
    assert pruning.prune_user_sql(sql, CATALOGO) == sql


# --- a poda nao pode PERDER ano pedido --------------------------------------

def test_range_de_varios_anos_mantem_todos():
    sql = _sel("din_instante >= '2023-01-01' AND din_instante < '2026-01-01'")
    out = pruning.prune_user_sql(sql, CATALOGO)
    assert out != sql
    arqs = _arquivos(out)
    for ano in ("2023", "2024", "2025"):
        assert ano in arqs, f"o ano {ano} esta no range e sumiu da poda"
