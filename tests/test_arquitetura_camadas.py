# -*- coding: utf-8 -*-
"""A regra de camadas do motor, como TESTE — nao como README.

Uma pergunta atravessa o sistema nesta ordem, e cada camada so' pode importar das
que vem ANTES dela:

    infra -> catalogo -> execucao -> resposta -> tools

Este teste congela isso. Sem ele, a organizacao degrada em silencio — bastaria um
`from ..tools import` dentro de `execucao/` e a fronteira sumiria sem ninguem ver.

O mapeamento e' por MODULO, nao por pasta: vale no layout plano de hoje e continua
valendo depois da reorganizacao em subpacotes (basta manter esta tabela).
"""

from __future__ import annotations

import ast
import io
from pathlib import Path

PKG = "mcp_tiago_dados_abertos"
SRC = Path(__file__).resolve().parents[1] / "src" / PKG

CAMADA_DE = {
    # infra: nao importa de ninguem
    "config": "infra", "reqctx": "infra", "telemetry": "infra", "toolaudit": "infra",
    # catalogo: contratos -> meta, ranking
    "contracts": "catalogo", "retrieval": "catalogo", "ranking": "catalogo",
    # execucao: validar, podar e rodar o SQL
    "validator": "execucao", "db": "execucao", "rate_limiter": "execucao", "pruning": "execucao",
    # resposta: SQL executado -> fatos tipados, avisos e rodape
    "result_schema": "resposta", "additivity": "resposta", "columns": "resposta",
    "provenance": "resposta", "unit_advisor": "resposta", "context": "resposta",
    # tools e servidor: orquestram
    "server": "tools", "buscar_dataset": "tools",
    "descrever_dataset": "tools", "executar_sql": "tools", "listar_datasets": "tools",
}
ORDEM = ["infra", "catalogo", "execucao", "resposta", "tools"]
NIVEL = {c: i for i, c in enumerate(ORDEM)}


def _imports_internos(path: Path) -> set[str]:
    """Modulos DO PACOTE que este arquivo importa (qualquer forma de import)."""
    tree = ast.parse(io.open(path, encoding="utf-8").read())
    alvos: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.module:
            partes = n.module.split(".")
            if n.level or partes[0] == PKG:
                folha = partes[-1]
                if folha in ORDEM or folha in ("tools", PKG):  # from pkg.<camada> import X -> X e' o modulo
                    alvos.update(a.name for a in n.names)
                else:
                    alvos.add(folha)
        elif isinstance(n, ast.Import):
            for a in n.names:
                if a.name.startswith(PKG + "."):
                    alvos.add(a.name.split(".")[-1])
    return alvos


def _modulos():
    for p in SRC.rglob("*.py"):
        if p.stem != "__init__":
            yield p


def test_todo_modulo_tem_camada():
    sem = sorted(p.stem for p in _modulos() if p.stem not in CAMADA_DE)
    assert not sem, f"modulo(s) sem camada declarada: {sem} — adicione em CAMADA_DE"


def test_nenhuma_camada_importa_de_cima():
    violacoes = []
    for p in _modulos():
        origem = CAMADA_DE[p.stem]
        for alvo in _imports_internos(p):
            if alvo in CAMADA_DE and NIVEL[CAMADA_DE[alvo]] > NIVEL[origem]:
                violacoes.append(f"{origem}/{p.stem} -> {CAMADA_DE[alvo]}/{alvo}")
    assert not violacoes, "dependencia para CIMA:\n  " + "\n  ".join(sorted(violacoes))


def test_infra_so_importa_infra():
    """A base nao conhece o motor: infra pode importar infra, nada acima."""
    for p in _modulos():
        if CAMADA_DE[p.stem] == "infra":
            acima = {a for a in _imports_internos(p) if a in CAMADA_DE and CAMADA_DE[a] != "infra"}
            assert not acima, f"infra/{p.stem} importa {sorted(acima)}"
