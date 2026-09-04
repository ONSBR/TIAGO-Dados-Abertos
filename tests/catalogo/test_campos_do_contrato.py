# -*- coding: utf-8 -*-
"""Regressao: campo declarado no contrato tem que CHEGAR ao motor.

Testa que campos declarados nos contratos sao carregados corretamente:

1. `examples` das colunas — usado pelo ranking ("valor provado no dado") e pela
   resolucao de valor de filtro (semantics_engine).

2. Vocabularios alternativos: `slaProperties.freshness` vs `slaProperties.frequency`,
   URL do portal em `authoritativeDefinitions` vs `customProperties.portalUrl`.
"""

from __future__ import annotations

import textwrap

import pytest

from mcp_tiago_dados_abertos.catalogo import catalog as cat


def _escreve_corpus(base, nome: str, corpo: str) -> None:
    d = base / "portalx"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{nome}.odcs.yaml").write_text(textwrap.dedent(corpo), encoding="utf-8")


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    """CONTRACTS_DIR temporario: sem cache, o carregador percorre os YAML."""
    monkeypatch.setattr(cat, "CONTRACTS_DIR", tmp_path)
    return tmp_path


CONTRATO = """\
    version: 1.0.0
    kind: DataContract
    apiVersion: v3.1.0
    id: urn:portalx:meu
    name: meu-dataset
    status: active
    tags: [teste]
    servers:
    - server: production
      type: s3
      location: s3://bucket/dataset/meu/*.parquet
    schema:
    - name: meu
      properties:
      - name: nom_regiao
        physicalType: VARCHAR
        description: Regiao.
        examples:
        - Guanabara
        - Tocantins
    authoritativeDefinitions:
    - url: https://dadosabertos.portalx.gov.br/dataset/meu
      type: businessDefinition
      description: Portal X
    slaProperties:
    - property: frequency
      value: P1M
    customProperties:
    - property: ragContext
      value: Dataset de teste.
"""


def test_examples_da_coluna_chegam_ao_catalogo(corpus):
    _escreve_corpus(corpus, "meu-dataset", CONTRATO)
    col = cat.load_all_contracts()["portalx/meu-dataset"]["columns"][0]
    assert col.get("examples") == ["Guanabara", "Tocantins"], (
        "o carregador descartou examples; quem le col['examples'] recebe vazio"
    )


def test_valor_so_dos_examples_pontua_no_ranking(corpus):
    """O sintoma real do bug 1: valor que existe SO em examples nao valia nada."""
    _escreve_corpus(corpus, "meu-dataset", CONTRATO)
    _escreve_corpus(corpus, "outro-dataset", CONTRATO.replace("meu-dataset", "outro-dataset"))
    c = cat.load_all_contracts()
    com_valor = cat.rank_datasets(c, "media em Guanabara", top_n=2)
    sem_valor = {n: s for s, n, _ in cat.rank_datasets(c, "media em Lisboa", top_n=2)}
    assert com_valor, "citar um valor real do dataset nao pontuou nada"
    assert com_valor[0][0] > sem_valor.get(com_valor[0][1], 0), (
        "o bonus de +10 por valor provado no dado nao aplicou a partir de examples"
    )


def test_freshness_aceita_o_vocabulario_frequency(corpus):
    _escreve_corpus(corpus, "meu-dataset", CONTRATO)
    meta = cat.load_all_contracts()["portalx/meu-dataset"]
    assert meta["freshness"] == "P1M", "slaProperties.frequency ignorado"


def test_portal_url_nao_e_especifico_do_ONS(corpus):
    _escreve_corpus(corpus, "meu-dataset", CONTRATO)
    meta = cat.load_all_contracts()["portalx/meu-dataset"]
    assert meta["portal_url"] == "https://dadosabertos.portalx.gov.br/dataset/meu", (
        "authoritativeDefinitions so era lido quando a URL era do dominio do ONS"
    )


def test_portal_url_cai_para_customProperty(corpus):
    """Contrato sem authoritativeDefinitions ainda pode declarar portalUrl."""
    corpo = CONTRATO.replace(
        """    authoritativeDefinitions:
    - url: https://dadosabertos.portalx.gov.br/dataset/meu
      type: businessDefinition
      description: Portal X
""",
        "",
    ).replace(
        """    - property: ragContext
      value: Dataset de teste.
""",
        """    - property: ragContext
      value: Dataset de teste.
    - property: portalUrl
      value: https://portalx.gov.br/d/meu
""",
    )
    _escreve_corpus(corpus, "meu-dataset", corpo)
    meta = cat.load_all_contracts()["portalx/meu-dataset"]
    assert meta["portal_url"] == "https://portalx.gov.br/d/meu"


def test_sem_nenhuma_das_duas_nao_inventa_url(corpus):
    """A regra que NAO pode mudar: sem definicao autoritativa, sem URL."""
    corpo = CONTRATO.replace(
        """    authoritativeDefinitions:
    - url: https://dadosabertos.portalx.gov.br/dataset/meu
      type: businessDefinition
      description: Portal X
""",
        "",
    )
    _escreve_corpus(corpus, "meu-dataset", corpo)
    assert cat.load_all_contracts()["portalx/meu-dataset"]["portal_url"] == ""


def test_corpus_de_um_contrato_nao_explode(corpus):
    """Catalogo com 1 contrato so. A busca tem que responder, nao levantar."""
    _escreve_corpus(corpus, "meu-dataset", CONTRATO)
    c = cat.load_all_contracts()
    assert cat.rank_datasets(c, "media por regiao", top_n=3)


# ── Duas convencoes de nome para o MESMO campo ────────────────────────────────
# Os mesmos 4 customProperties existem em duas grafias, portugues e ingles camelCase
# (`ragContext` / `aiContext`, `periodoCoberturaInicio` / `temporalCoverageStart`,
# ...). Um carregador que le so' uma delas faz o contrato na outra chegar ao motor
# com rag_context VAZIO — e campo vazio nao levanta erro, some calado. Como
# `rag_context` e' o texto mais rico do indice de busca, o dataset fica
# praticamente invisivel. Ha' precedente no proprio carregador: `parquetSource`
# e `sourceExpression` ja' sao aceitos como sinonimos.

CONTRATO_INGLES = CONTRATO.replace(
    """    - property: ragContext
      value: Dataset de teste.""",
    """    - property: aiContext
      value: Serie de vazao afluente com regularizacao plurianual.
    - property: temporalCoverageStart
      value: '2020-01-01'
    - property: temporalCoverageEnd
      value: '2024-12-31'""",
)


def test_convencao_antiga_continua_valendo(corpus):
    _escreve_corpus(corpus, "meu-dataset", CONTRATO)
    meta = cat.load_all_contracts()["portalx/meu-dataset"]
    assert meta["rag_context"] == "Dataset de teste."


def test_convencao_nova_chega_ao_motor(corpus):
    """aiContext/temporalCoverage* sao os mesmos campos com outro nome."""
    _escreve_corpus(corpus, "meu-dataset", CONTRATO_INGLES)
    meta = cat.load_all_contracts()["portalx/meu-dataset"]
    assert meta["rag_context"].startswith("Serie de vazao afluente"), (
        "aiContext nao chegou: o dataset fica sem o texto mais rico do indice de busca"
    )
    assert meta["period_start"] == "2020-01-01"
    assert meta["period_end"] == "2024-12-31"


def test_dataset_migrado_continua_recuperavel(corpus):
    """O sintoma real: a pergunta casa SO' pelo texto do aiContext — nem nome, nem tag.

    Sem o conserto o dataset nao e' recuperado por essas palavras, que e' como o
    ONS inteiro sumiria da busca depois da migracao de nomes.
    """
    _escreve_corpus(corpus, "meu-dataset", CONTRATO_INGLES)
    _escreve_corpus(corpus, "outro", CONTRATO_INGLES.replace("meu-dataset", "outro"))
    c = cat.load_all_contracts()
    achados = [n for _s, n, _m in cat.rank_datasets(c, "vazao afluente regularizacao", top_n=2)]
    assert achados, "corpus migrado ficou invisivel: a busca nao ve o texto do aiContext"
