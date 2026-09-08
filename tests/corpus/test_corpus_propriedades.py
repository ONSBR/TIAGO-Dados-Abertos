# -*- coding: utf-8 -*-
"""Uma convencao de nome, e nome errado falha alto.

O carregador le cada `customProperties` por nome exato. Um nome que ele nao
conhece nao levanta erro: `custom.get(...)` devolve o default e o campo chega
VAZIO ao motor. Como `aiContext` e' o texto mais rico do indice de busca, um
contrato com o nome errado fica praticamente invisivel — e nada avisa.

A defesa antiga contra isso era aceitar duas grafias do mesmo campo, portugues
e ingles. So' que os quatro pares tinham o lado portugues em ZERO dos 78
contratos, enquanto a documentacao mandava usar justamente esse lado. Foi assim
que o auditor de cobertura nasceu escrevendo contra `periodoCoberturaFim`,
detectando os defasados e nao corrigindo nenhum.

Aqui a tolerancia vira o contrario: uma convencao so', e o nome fora dela
reprova na hora, com a lista do que era esperado.
"""
import re
from pathlib import Path

CONTRACTS_DIR = Path(__file__).resolve().parents[2] / "contracts" / "ons"
LOADER = (Path(__file__).resolve().parents[2] / "src" / "mcp_tiago_dados_abertos"
          / "catalogo" / "contracts.py")

# Tudo que o carregador le do bloco raiz de customProperties.
CONHECIDAS = {
    "aiContext", "agentInstructions", "typicalQuestions", "fewShotQueries",
    "granularity", "spatialGranularity", "temporalCoverageStart",
    "temporalCoverageEnd", "sourceExpression", "partitionPruning",
    "slaAtualizacao", "primaryKey", "vectorTags", "portalUrl", "semantics",
    # nao chega ao motor, mas e' metadado legitimo do contrato
    "license",
}


def _raiz(doc):
    return [x.get("property") for x in (doc.get("customProperties") or [])]


def test_todo_contrato_usa_a_convencao_do_carregador(corpus_docs):
    """Nome fora da convencao esvazia o campo em silencio."""
    intrusos = []
    for ds, doc in corpus_docs:
        for p in _raiz(doc):
            if p not in CONHECIDAS:
                intrusos.append(f"{ds}: {p}")
    assert not intrusos, (
        "customProperties que o carregador nao le (%d):\n  %s\n\n"
        "O campo chegaria vazio ao motor sem erro nenhum. Use um destes: %s"
        % (len(intrusos), "\n  ".join(intrusos[:15]), ", ".join(sorted(CONHECIDAS))))


def test_conhecidas_cobre_o_que_o_carregador_le():
    """A lista acima nao pode ficar para tras do codigo.

    Se alguem passar a ler um campo novo em contracts.py e esquecer daqui, o
    lint reprovaria um contrato correto. Este teste le o proprio carregador.
    """
    fonte = LOADER.read_text(encoding="utf-8")
    lidas = set(re.findall(r'custom\.get\(\s*"([A-Za-z_]+)"', fonte))
    faltando = lidas - CONHECIDAS
    assert not faltando, (
        f"contracts.py le campos que CONHECIDAS nao lista: {sorted(faltando)}")


def test_nenhuma_grafia_legada_no_corpus(corpus_docs):
    """A convencao antiga saiu do carregador; nao pode voltar pelo corpus.

    Os quatro nomes abaixo eram aceitos como sinonimo e nao eram usados por
    contrato nenhum. Sem o sinonimo, um contrato que os use perde o campo.
    """
    legadas = {"ragContext", "parquetSource", "periodoCoberturaInicio", "periodoCoberturaFim"}
    achados = []
    for ds, doc in corpus_docs:
        for p in _raiz(doc):
            if p in legadas:
                achados.append(f"{ds}: {p}")
    assert not achados, f"grafia legada no corpus: {achados}"
