# -*- coding: utf-8 -*-
"""Os 78 contratos parseados UMA vez, com o mesmo loader que o servidor usa.

Dois desperdicios somados faziam a suite de corpus levar minutos:

  - `yaml.safe_load` nunca usa o loader em C. Nos 78 contratos sao 10,1s contra
    1,0s do CSafeLoader — o proprio carregador ja escolhe o rapido por esse
    motivo (catalogo/contracts.py), a suite e' que nao seguia.
  - cada teste reparseava o corpus inteiro. Com 5 testes, 50s so' de parse
    repetido para responder perguntas sobre os MESMOS arquivos.

Escopo de sessao: o parse acontece uma vez por execucao do pytest.
"""
from pathlib import Path

import pytest
import yaml

CONTRACTS_DIR = Path(__file__).resolve().parents[2] / "contracts" / "ons"

# yaml.safe_load NUNCA usa o loader em C; tem que pedir explicitamente.
_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


@pytest.fixture(scope="session")
def corpus_docs():
    """[(nome_do_dataset, doc)] dos 78 contratos, na ordem do nome.

    Os docs sao COMPARTILHADOS entre os testes: leia, nunca modifique.
    """
    out = []
    for f in sorted(CONTRACTS_DIR.glob("*.yaml")):
        doc = yaml.load(f.read_text(encoding="utf-8"), Loader=_LOADER)
        out.append((f.name.replace(".odcs.yaml", ""), doc))
    return out
