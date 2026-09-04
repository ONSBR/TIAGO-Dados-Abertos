# -*- coding: utf-8 -*-
"""Tool `listar_datasets`: o catalogo inteiro em uma linha por dataset.

Complementa `buscar_dataset`: a busca e' determinista e barata, mas decide por termos; quando
a pergunta e' vaga, usa sinonimo que nenhum contrato tem, ou cita o dataset pelo nome, quem
decide melhor e' o modelo cliente lendo a lista completa. A linha traz o que ele nao consegue
inferir do nome: granularidade, perspectiva temporal (verificado, programado, previsto,
cadastral) e uma frase de uso. Cerca de 3 mil tokens para o corpus inteiro; sem argumentos,
mesma saida para todo mundo, entao serve tambem como resource.
"""
from __future__ import annotations

_FRASE_MAX = 110


def _frase(meta: dict) -> str:
    texto = " ".join(str(meta.get("rag_context") or meta.get("description") or "").split())
    for prefixo in ("Recuperar este dataset quando a pergunta envolver ", "Recuperar este dataset quando "):
        if texto.startswith(prefixo):
            texto = texto[len(prefixo):]
            break
    if len(texto) > _FRASE_MAX:
        corte = texto[:_FRASE_MAX].rsplit(" ", 1)[0]
        texto = corte.rstrip(",;:") + "..."
    return texto


def listar_datasets(*, catalog: dict, orgao: str | None = None) -> str:
    """Uma linha por dataset ativo: `[ORGAO] nome | granularidade | perspectiva | uso`."""
    orgao_f = (orgao or "").strip().lower() or None
    linhas = []
    for key, meta in sorted(catalog.items(), key=lambda kv: kv[1].get("name", kv[0])):
        if meta.get("status") == "discontinued":
            continue
        if orgao_f and meta.get("orgao") != orgao_f:
            continue
        sem = meta.get("semantics") or {}
        persp = str(sem.get("temporal_perspective") or "").strip() or "-"
        gran = str(meta.get("granularity") or "").strip() or "-"
        org = str(meta.get("orgao") or "").upper()
        linhas.append(f"- [{org}] `{meta.get('name', key)}` | {gran} | {persp} | {_frase(meta)}")
    if not linhas:
        return "Nenhum dataset" + (f" no portal {orgao_f.upper()}." if orgao_f else " no catalogo.")
    cab = (
        f"## Catalogo: {len(linhas)} datasets\n\n"
        "Formato: nome | granularidade | perspectiva (verificado = medido; programado/previsto = "
        "planejamento; cadastral = inventario) | uso. Escolha pelo assunto E pelo grao da pergunta; "
        "depois chame descrever_dataset(nome) antes de gerar SQL.\n"
    )
    return "\n".join([cab] + linhas)
