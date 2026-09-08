# -*- coding: utf-8 -*-
"""Testes adicionais de regressao do validador SQL.

Casos de rejeicao e aceitacao para payloads de funcoes internas do DuckDB,
literais gigantes e buckets nao autorizados. Os de REJECT devem falhar;
os de PASS garantem que o validador nao super-bloqueia queries legitimas.
"""


import pytest

from mcp_tiago_dados_abertos.execucao.validator import SqlValidator


@pytest.fixture
def validator():
    return SqlValidator(con=None)


# Payloads que devem ser rejeitados
REJECT = [
    ("SELECT duckdb_execute('DROP TABLE x')", "duckdb_ prefix fn"),
    ("SELECT duckdb_prepare('x')", "duckdb_prepare"),
    ("SELECT duckdb_current_setting('s3_secret_access_key')", "duckdb_current_setting exfil"),
    ("SELECT duckdb_files()", "duckdb_files"),
    ("SELECT repeat('x', 1000000 * 1000000)", "literal gigante computado (arg times)"),
    ("SELECT range(1, 1000000 * 100000)", "literal gigante computado em range"),
    ("SELECT * FROM read_parquet('s3://example-blocked-bucket/secret.parquet')", "bucket irmao via prefixo largo"),
    ("SELECT * FROM read_parquet('s3://ons-aws-prod-opendata.evil.com/x.parquet')", "domain squat"),
]

# Validos que NAO podem ser bloqueados (guarda contra over-block)
PASS = [
    ("SELECT * FROM read_parquet('s3://ons-aws-prod-opendata/ds/x.parquet') LIMIT 5", "bucket ONS real"),
    ("SELECT md5(x) AS h FROM (SELECT 'a' AS x)", "md5 analitica"),
    ("SELECT range(1, 1000) AS r", "range pequeno ok"),
    ("SELECT repeat('-', 40) AS sep", "repeat pequeno ok"),
]


@pytest.mark.parametrize("sql,desc", REJECT)
def test_validator_extra_rejected(validator, sql, desc):
    ok, _ = validator.is_safe(sql)
    assert not ok, f"deveria REJEITAR ({desc}): {sql}"


@pytest.mark.parametrize("sql,desc", PASS)
def test_validator_extra_valid_passes(validator, sql, desc):
    ok, msg = validator.is_safe(sql)
    assert ok, f"nao deveria bloquear ({desc}): {sql} -> {msg}"


def test_comentario_inicial_nao_rejeita(validator):
    # O LLM adora prefixar com "-- titulo da consulta"; rejeitar custa um
    # round-trip inteiro (boot frio + retry). Comentario inicial e legal em SQL.
    ok, msg = validator.validate(
        "-- Geracao total por fonte em 2025\n"
        "/* bloco */ SELECT 1 FROM read_parquet("
        "'s3://ons-aws-prod-opendata/dataset/x/*.parquet') WHERE a >= '2025-01-01'"
    )
    assert ok, msg


def test_comentario_nao_vira_bypass(validator):
    # comentario inicial nao pode abrir porta p/ DDL depois dele
    ok, _ = validator.validate("-- inocente\nDROP TABLE x")
    assert not ok
