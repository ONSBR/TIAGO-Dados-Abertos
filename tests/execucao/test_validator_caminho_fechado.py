# -*- coding: utf-8 -*-
r"""O validador SQL falha FECHADO no caminho do leitor e no host HTTPS.

Revisao adversarial com impacto PROVADO contra o DuckDB real — alvos inofensivos
(`.invalid` nunca resolve; bucket inexistente), observando qual host o DuckDB
tentou contatar:

- Leitura de bucket S3 ARBITRARIO chegou a `GET bucket-alheio.s3.amazonaws.com`
  por quatro vetores: `FROM 's3://...'` sem funcao de leitor, caminho montado com
  `chr()||`, string `$$...$$`, string `E'...'`.
- SSRF para host HTTPS arbitrario chegou a `HEAD https://evil.invalid/...` por
  quatro vetores: URL montada com `chr()`, e tres formas de confusao de
  autoridade — `evil#@dados.ons.org.br`, `?@`, `\@`. O validador via
  `dados.ons.org.br`; o DuckDB conectou em `evil`.

Duas causas, distintas:
1. A checagem de caminho era POR LITERAL e falhava ABERTA: sem `exp.Literal`
   extraivel do argumento (RawString, ByteString, DPipe, Subquery), a lista de
   caminhos ficava vazia e o vazio era aprovado. `FROM 'string'` nem passava por
   ela — e' Table, nao leitor — o que tambem abria LFI por caminho RELATIVO
   (`FROM 'x.parquet'`), fora do alcance da regex de caminho local.
2. A checagem de host nao era um parser de URL: cortava no ULTIMO `@`, ignorando
   que `#`, `?` e `\` encerram a autoridade antes.

Regra agora: argumento de leitor e' literal de string simples (ou array so' de
literais) — qualquer outra forma e' rejeitada; tabela cujo nome parece caminho ou
URL e' rejeitada (aqui so' existe leitor explicito e CTE); host vem de urlsplit e a
autoridade nao pode conter `@ # ? \`.
"""

from __future__ import annotations

import pytest

from mcp_tiago_dados_abertos.execucao.validator import SqlValidator

v = SqlValidator(None)


def _chrs(s: str) -> str:
    return "||".join(f"chr({ord(c)})" for c in s)


BLOQUEAR = {
    "tabela implicita s3": "SELECT * FROM 's3://bucket-alheio/x.parquet'",
    "tabela implicita https": "SELECT * FROM 'https://evil.invalid/x.csv'",
    "tabela implicita local absoluta": "SELECT * FROM '/etc/passwd'",
    "tabela implicita local RELATIVA (LFI)": "SELECT * FROM 'x.parquet'",
    "tabela implicita glob relativo": "SELECT * FROM '*.csv'",
    "caminho so' com chr() s3": f"SELECT * FROM read_parquet({_chrs('s3://bucket-alheio/x.parquet')})",
    "caminho so' com chr() https": f"SELECT * FROM read_csv({_chrs('https://evil.invalid/x.csv')})",
    "caminho por subquery": "WITH p AS (SELECT 's3://bucket-alheio/x' AS u) SELECT * FROM read_parquet((SELECT u FROM p))",
    "caminho por concat de literais": "SELECT * FROM read_parquet('s3://ons-aws-prod-opendata/' || 'x.parquet')",
    "dollar-quoted s3": "SELECT * FROM read_parquet($$s3://bucket-alheio/x.parquet$$)",
    "dollar-quoted https": "SELECT * FROM read_csv($$https://evil.invalid/x.csv$$)",
    "E-string s3": "SELECT * FROM read_parquet(E's3://bucket-alheio/x.parquet')",
    "autoridade com #@": "SELECT * FROM read_csv('https://evil.invalid#@dados.ons.org.br/x.csv')",
    "autoridade com ?@": "SELECT * FROM read_csv('https://evil.invalid?@dados.ons.org.br/x.csv')",
    r"autoridade com \@": r"SELECT * FROM read_csv('https://evil.invalid\@dados.ons.org.br/x.csv')",
    "userinfo simples": "SELECT * FROM read_csv('https://dados.ons.org.br@evil.invalid/x.csv')",
    "squat de sufixo": "SELECT * FROM read_csv('https://dados.ons.org.br.evil.invalid/x.csv')",
    "hf://": "SELECT * FROM read_parquet('hf://datasets/x/y.parquet')",
    "http cleartext em host permitido": "SELECT * FROM read_csv('http://dados.ons.org.br/x.csv')",
    "lista mista": "SELECT * FROM read_parquet(['s3://ons-aws-prod-opendata/a.parquet','s3://bucket-alheio/b.parquet'])",
    "lista com elemento nao-literal": f"SELECT * FROM read_parquet(['s3://ons-aws-prod-opendata/a.parquet', {_chrs('s3://x/b')}])",
}

PERMITIR = {
    "leitor literal s3 ONS": "SELECT * FROM read_parquet('s3://ons-aws-prod-opendata/dataset/x/*.parquet') LIMIT 1",
    "leitor literal com opcoes": "SELECT * FROM read_parquet('s3://ons-aws-prod-opendata/dataset/x/*.parquet', union_by_name=true, filename=true)",
    "array de literais": "SELECT * FROM read_parquet(['s3://ons-aws-prod-opendata/a.parquet','s3://ons-aws-prod-opendata/b.parquet'], union_by_name=true)",
    "https host permitido": "SELECT * FROM read_csv('https://dados.ons.org.br/x.csv')",
    "https com porta": "SELECT * FROM read_csv('https://dados.ons.org.br:443/x.csv')",
    "CTE com nome simples": "WITH t AS (SELECT 1 AS a) SELECT a FROM t",
    "valor 'E' num filtro nao e' E-string": "SELECT * FROM read_parquet('s3://ons-aws-prod-opendata/x.parquet') WHERE id_estado = 'E'",
    "maiuscula no esquema": "SELECT * FROM read_parquet('S3://ons-aws-prod-opendata/x.parquet')",
}


@pytest.mark.parametrize("sql", BLOQUEAR.values(), ids=list(BLOQUEAR))
def test_bloqueia(sql):
    ok, msg = v.is_safe(sql)
    assert not ok, f"PASSOU e nao devia: {sql}"
    assert msg, "bloqueio sem motivo"


@pytest.mark.parametrize("sql", PERMITIR.values(), ids=list(PERMITIR))
def test_permite(sql):
    ok, msg = v.is_safe(sql)
    assert ok, f"bloqueou SQL legitimo: {sql}\n{msg}"
