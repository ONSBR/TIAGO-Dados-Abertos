# -*- coding: utf-8 -*-
"""Conformidade com o Open Data Contract Standard, contra o schema OFICIAL.

O corpus e' o produto: se um contrato deixa de ser ODCS valido, qualquer
ferramenta de terceiro que o leia quebra, e o projeto perde a razao de usar um
padrao aberto em vez de um formato proprio.

A validacao usa o JSON Schema publicado no pacote `open-data-contract-standard`,
nao uma copia no repositorio: copia envelhece em silencio. Se o pacote nao
estiver instalado o teste e' pulado, do mesmo jeito que os testes que dependem
de rede.

Nota sobre o modelo Python do mesmo pacote: ele declara TUDO opcional, entao
construir OpenDataContractStandard(**doc) passa em qualquer coisa e nao vale
como validacao. O `required` de verdade esta' so' no JSON Schema.
"""
from pathlib import Path

import pytest
import yaml

jsonschema = pytest.importorskip("jsonschema")
odcs = pytest.importorskip(
    "open_data_contract_standard",
    reason="instale open-data-contract-standard para validar conformidade ODCS")

import json  # noqa: E402

CONTRACTS_DIR = Path(__file__).resolve().parents[2] / "contracts" / "ons"
LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)

# Os quatro que o padrao exige no topo. Ficam explicitos aqui para o teste falhar
# com uma mensagem util em vez de um erro de schema cru.
OBRIGATORIOS = ("version", "apiVersion", "kind", "id")


@pytest.fixture(scope="session")
def validador():
    esquema = json.loads(
        (Path(odcs.__file__).parent / "schema.json").read_text(encoding="utf-8"))
    return jsonschema.Draft202012Validator(esquema)


def test_todo_contrato_tem_os_campos_que_o_padrao_exige(corpus_docs):
    faltando = []
    for ds, doc in corpus_docs:
        ausentes = [c for c in OBRIGATORIOS if not doc.get(c)]
        if ausentes:
            faltando.append(f"{ds}: falta {', '.join(ausentes)}")
    assert not faltando, "campo obrigatorio do ODCS ausente:\n  " + "\n  ".join(faltando)


def test_todo_contrato_valida_contra_o_schema_oficial(corpus_docs, validador):
    problemas = []
    for ds, doc in corpus_docs:
        for e in sorted(validador.iter_errors(doc), key=lambda x: list(x.absolute_path))[:3]:
            caminho = "/".join(str(x) for x in e.absolute_path) or "(topo)"
            problemas.append(f"{ds} em {caminho}: {e.message[:120]}")
    assert not problemas, (
        "contrato fora do ODCS (%d):\n  %s" % (len(problemas), "\n  ".join(problemas[:15])))


def test_apiVersion_declarada_e_aceita_pelo_padrao(corpus_docs, validador):
    """Versao inventada passaria despercebida no resto da validacao."""
    aceitas = set(validador.schema["properties"]["apiVersion"]["enum"])
    fora = sorted({doc.get("apiVersion") for _, doc in corpus_docs} - aceitas)
    assert not fora, f"apiVersion fora do enum do padrao: {fora} (aceitas: {sorted(aceitas)})"


def test_uma_apiVersion_so_no_corpus(corpus_docs):
    """Corpus com duas versoes do padrao valida em ambas e confunde quem consome."""
    versoes = sorted({doc.get("apiVersion") for _, doc in corpus_docs})
    assert len(versoes) == 1, f"o corpus mistura versoes do ODCS: {versoes}"
