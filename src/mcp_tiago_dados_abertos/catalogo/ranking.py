# -*- coding: utf-8 -*-
"""Ranking dos datasets para uma pergunta: keyword + TF-IDF + intencao de inventario.

Era a segunda metade de catalog.py; carregar contrato e ranquear pergunta sao
responsabilidades distintas. `catalog.rank_datasets` continua valendo (reexport).
"""
from __future__ import annotations

import re
from typing import Optional

# Pesos de ranqueamento (keyword + semantico). O semantico (TF-IDF) tende a acertar
# o intent mas tem cosseno baixo (~0.1); o keyword domina. Estes pesos calibram quanto
# confiar no semantico.
_SEM_WEIGHT = 30  # multiplica o cosseno (sinal continuo, fraco) — dobrado vs keyword
_SEM_RANK_MULT = 6  # boost discreto por estar no top-N semantico (sinal forte de intent)
_SEM_RANK_TOPN = 3
# Conviccao do indice: o bonus de posicao so vale quando o TF-IDF de fato separou
# alguem. Cosseno do 1o abaixo do piso, ou 1o e 4o colados, e' distribuicao achatada:
# nela o bonus (ate 18 pts) decidia a pergunta sobre diferencas de 0,01 de cosseno.
# Nas perguntas tipicas: p10 do cos1 = 0,197 e p10 da margem cos1-cos4 = 0,092; nas
# perguntas neutras de usuario, cos1 0,07-0,14 e margem <= 0,07.
# Grade medida (piso x margem): 0,12 x 0,03 mantem 356/384 nas tipicas e acerta as 8
# perguntas neutras do caso; 0,15 ja perde 3 tipicas e a de previsao de carga.
_SEM_GATE_MIN_COS = 0.12
_SEM_GATE_MARGIN = 0.03


# ── Intent de INVENTARIO ───────────────────────────────────────────────────────
# "Quais equipamentos existem" pede o CADASTRO; "disponibilidade em 2024" pede o
# indicador. A pergunta de inventario caia no dataset de serie temporal e voltava
# numero de desempenho no lugar da lista.
#
# 'qual' (singular) NAO entra: "qual a carga ontem" e' serie temporal.
_INVENTARIO_WORDS = frozenset(
    {"quais", "quantos", "quantas", "lista", "listar", "liste", "listagem",
     "cadastro", "existem", "inventario", "relacao"}
)

# Marcas de periodo. Com qualquer uma delas a pergunta e' temporal e a regra de
# inventario NAO se aplica ("quais subsistemas tiveram mais carga em 2024").
_PERIODO_WORDS = frozenset(
    {"ontem", "hoje", "anteontem", "amanha", "dia", "dias", "semana", "semanas",
     "mes", "meses", "ano", "anos", "trimestre", "semestre", "diario", "diaria",
     "mensal", "anual", "semanal", "horario", "horaria", "serie", "evolucao",
     "historico", "tendencia", "ultimo", "ultimos", "ultima", "ultimas",
     "periodo", "desde", "entre", "durante"}
)
_ANO_RE = re.compile(r"(?<!\d)(19|20)\d{2}(?!\d)")
# Palavras de conversa que pergunta real traz e contrato nenhum discrimina ("voce tem dados
# sobre..."). Sem isto, "tem" casava "sistema" e "sobre" casava "sobrecarga" por substring,
# e a pergunta ia para o dataset errado.
_STOPWORDS_CONVERSA = frozenset({
    "voce", "vc", "tem", "tenho", "temos", "tens", "sobre", "quero", "queria", "gostaria", "preciso",
    "saber", "informacao", "informacoes", "dados", "dado", "me", "meu", "minha", "tiago", "esta",
    "estao", "existe", "existem", "consegue", "consigo", "pode", "poderia", "passar", "mostrar",
    "mostre", "traga", "trazer", "liste", "lista", "listar", "diga", "fale", "ola", "oi", "bom",
    "boa", "tarde", "noite", "favor", "obrigado", "obrigada", "seria", "sao", "esse", "essa",
    "esses", "essas", "este", "isso", "aqui", "tambem", "entao", "agora", "hoje", "ontem",
})
# Token com menos que isto so casa palavra inteira (ver _casa em rank_datasets).
_MIN_SUBSTR_LEN = 5
# Teto de tags casadas que pontuam por contrato. Sem teto, a pontuacao de tag vira contagem:
# dez sinonimos com a palavra 'linha' valiam +30 ao cadastro de linhas em QUALQUER pergunta
# que citasse linha, e enriquecer o vocabulario de um contrato virava repesa-lo. Sem teto,
# encher vectorTags nos 77 contratos custava 11 tipicas; com teto 8, ganha em top-1, top-3,
# top-5 E tipicas.
_TAG_HITS_CAP = 8
# Perspectiva temporal (semantics.temporal_perspective): o que a linha do dataset E'.
# Pergunta que pede previsao/programacao quer PROGRAMADO/PREVISTO; que pede verificado
# quer VERIFICADO; pergunta NEUTRA sobre uma grandeza quer o verificado, entao o
# programado so entra por pedido. Cadastral fica de fora: nao concorre nesse eixo.
_PERSPECTIVA_FUTURO = frozenset({"programado", "previsto"})
_PEDE_FUTURO = frozenset({
    "programado", "programada", "programados", "programadas", "programacao",
    "previsto", "prevista", "previstos", "previstas", "previsao", "previsoes",
    "planejado", "planejada", "planejamento", "dessem", "pmo", "pdp", "amanha",
})
_PEDE_VERIFICADO = frozenset({
    "verificado", "verificada", "verificados", "verificadas", "realizado", "realizada",
    "realizados", "realizadas", "medido", "medida", "medidos", "medidas", "efetivo", "efetiva",
})
_PERSPECTIVA_PESO = 6  # mesma magnitude de 'pediu ranking e o dataset nao tem metrica'


def rank_datasets(catalog: dict, pergunta: str, top_n: int = 3, orgao: Optional[str] = None) -> list:
    """Ranqueia datasets por relevancia combinando keyword + semantico.
    Se orgao informado, filtra por orgao.
    """
    import unicodedata

    from mcp_tiago_dados_abertos.catalogo.retrieval import ensure_semantic_index, semantic_search

    ensure_semantic_index(catalog)

    def _normalize(text: str) -> str:
        """Remove acentos e converte para lowercase."""
        return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii").lower()

    tokens = set(re.findall(r"\b\w+\b", _normalize(pergunta)))
    stopwords = {
        "de",
        "do",
        "da",
        "dos",
        "das",
        "em",
        "no",
        "na",
        "nos",
        "nas",
        "por",
        "para",
        "com",
        "o",
        "a",
        "os",
        "as",
        "e",
        "ou",
        "que",
        "qual",
        "quais",
        "como",
        "foi",
        "ser",
        "ter",
        "ano",
        "mes",
        "dia",
    }
    # ANTES de tirar stopwords: 'quais'/'quantos' SAO stopwords aqui, e sao
    # exatamente o sinal de inventario. Ler os tokens crus e' o unico jeito.
    tokens_crus = set(tokens)
    tokens = tokens - stopwords - _STOPWORDS_CONVERSA
    # Tokens com <3 chars sao ruido no match por SUBSTRING: "ta" (de "quanto ta")
    # casa "importacao"/"Portaria"/"public_data" e afoga o sinal correto.
    tokens = {t for t in tokens if len(t) >= 3}
    tokens = tokens | {t.rstrip("s") for t in tokens if len(t) > 3}

    def _casa(t: str, texto: str) -> bool:
        # Token curto so casa palavra inteira: "tem" dentro de "sistema" e "sub" dentro de
        # "subestacao" davam +3 por tag a quem nao tinha nada a ver com a pergunta.
        if len(t) >= _MIN_SUBSTR_LEN:
            return t in texto
        return re.search(r"\b" + re.escape(t) + r"\b", texto) is not None

    # Uma vez, fora do laco: a pergunta fala de periodo?
    tem_periodo = bool(tokens_crus & _PERIODO_WORDS) or bool(_ANO_RE.search(pergunta))
    quer_inventario = bool(tokens_crus & _INVENTARIO_WORDS) and not tem_periodo

    kw_scores = {}
    for key, meta in catalog.items():
        if meta.get("status") == "discontinued":
            continue
        if orgao and meta.get("orgao") != orgao:
            continue
        score = 0
        # Tags (portal tags + vector_tags)
        all_tags = list(meta.get("tags", [])) + list(meta.get("vector_tags", []))
        tags_norm = [_normalize(tag) for tag in all_tags]
        _n_tags = sum(1 for tg in tags_norm if any(_casa(t, tg) for t in tokens))
        score += 3 * min(_n_tags, _TAG_HITS_CAP)
        rag = _normalize(meta.get("rag_context", ""))
        for t in tokens:
            if _casa(t, rag):
                score += 2
        name_lower = _normalize(meta["name"]).replace("-", " ").replace("_", " ")
        name_matches = sum(1 for t in tokens if _casa(t, name_lower))
        if name_matches >= 2:
            score += name_matches * 4
        elif name_matches == 1:
            score += 2
        for q in meta.get("typical_questions", []):
            matching = sum(1 for t in tokens if _casa(t, _normalize(q)))
            if matching >= 2:
                score += matching
        if not meta.get("granularity") and len(meta.get("columns", [])) < 5:
            score = max(0, score - 4)
        ranking_words = {"top", "maior", "menor", "ranking", "melhor", "pior", "total", "soma", "media"}
        routing = (meta.get("semantics") or {}).get("query_routing", {})
        if tokens & ranking_words:
            has_metric = any(c.get("semantic_type") == "METRIC" or c.get("unit") for c in meta.get("columns", []))
            if not has_metric:
                score = max(0, score - 6)
            # query_routing declara explicitamente se o dataset suporta ranking
            if routing.get("supports_ranking") is True:
                score += 4
            elif routing.get("supports_ranking") is False:
                score = max(0, score - 4)
        if (tokens & {"serie", "evolucao", "historico", "tendencia"}) and routing.get("supports_time_series") is True:
            score += 3
        # Inventario: o cadastro ganha, o indicador de serie perde. O sinal vem de
        # row_grain.temporal/primary_use_case — supports_listing NAO discrimina
        # (e' true em 67 dos 78 contratos, inclusive nos de serie pura).
        if quer_inventario:
            grain = (meta.get("semantics") or {}).get("row_grain") or {}
            estatico = (
                str(grain.get("temporal", "")).strip().upper() == "STATIC"
                or routing.get("primary_use_case") == "cadastro"
            )
            # +-8 (era +-6): responder "o que existe" com um indicador mensal e' erro
            # de CATEGORIA, nao aproximacao. Mesma magnitude que o codigo ja' usa
            # para "pediu ranking e o dataset nao tem metrica".
            if estatico:
                score += 8
            elif routing.get("supports_time_series") is True:
                score = max(0, score - 8)
        # Perspectiva temporal: ver _PEDE_FUTURO/_PEDE_VERIFICADO. Custo zero nas 384
        # custo zero nas 384 perguntas tipicas; 'carga prevista para amanha' passa a ir
        # ao DESSEM e 'geracao de energia 2025' deixa de cair no programado.
        perspectiva = str(((meta.get("semantics") or {}).get("temporal_perspective") or "")).strip().lower()
        if perspectiva in _PERSPECTIVA_FUTURO or perspectiva == "verificado":
            futuro = perspectiva in _PERSPECTIVA_FUTURO
            if tokens & _PEDE_FUTURO:
                score = score + _PERSPECTIVA_PESO if futuro else max(0, score - _PERSPECTIVA_PESO)
            elif tokens & _PEDE_VERIFICADO:
                score = max(0, score - _PERSPECTIVA_PESO) if futuro else score + _PERSPECTIVA_PESO
            elif futuro:
                score = max(0, score - _PERSPECTIVA_PESO)
        # Valor PROVADO no dado: a pergunta citar um valor real do dataset e
        # evidencia quase-decisiva. Fontes: validated_params (validacao por execucao)
        # e enum/examples das colunas. +10 UMA vez (flag achou_valor).
        sem_meta = meta.get("semantics") or {}
        pnorm = _normalize(pergunta)
        achou_valor = False
        for b in (sem_meta.get("sql_patterns") or {}).values():
            if achou_valor or not isinstance(b, dict):
                continue
            for v in (b.get("validated_params") or {}).values():
                vs = _normalize(str(v))
                if len(vs) >= 4 and not vs.isdigit() and vs in pnorm:
                    score += 10
                    achou_valor = True
                    break
        if not achou_valor:
            for c in meta.get("columns", []):
                if achou_valor:
                    break
                for v in (c.get("enum") or []) + (c.get("examples") or []):
                    vs = _normalize(str(v))
                    if len(vs) >= 4 and not vs.isdigit() and vs in pnorm:
                        score += 10
                        achou_valor = True
                        break
        kw_scores[key] = score

    sem_results = semantic_search(pergunta, top_n=10)
    sem_scores = {name: s for s, name, _ in sem_results}

    conviccao = bool(sem_results) and sem_results[0][0] >= _SEM_GATE_MIN_COS and (
        len(sem_results) <= _SEM_RANK_TOPN
        or sem_results[0][0] - sem_results[_SEM_RANK_TOPN][0] >= _SEM_GATE_MARGIN
    )
    top_sem = {n: i for i, (s, n, _) in enumerate(sem_results[:_SEM_RANK_TOPN])} if conviccao else {}
    combined = {}
    for key in set(kw_scores.keys()) | set(sem_scores.keys()):
        # o indice semantico e por processo: chave que nao esta NESTE catalogo (indice
        # construido sobre outro catalogo) nao pode ranquear
        if key not in catalog:
            continue
        # filtro por orgao vale tambem para o caminho SEMANTICO (antes so o keyword
        # respeitava: buscar(orgao=X) devolvia dataset de outro orgao via embedding)
        if orgao and (catalog.get(key) or {}).get("orgao") != orgao:
            continue
        if (catalog.get(key) or {}).get("status") == "discontinued":
            continue
        kw = kw_scores.get(key, 0)
        sem = sem_scores.get(key, 0.0)
        rank_boost = (_SEM_RANK_TOPN - top_sem[key]) * _SEM_RANK_MULT if key in top_sem else 0
        combined[key] = kw + (sem * _SEM_WEIGHT) + rank_boost

    # Desempate DETERMINISTICO: score desc, depois chave (nome) asc — estavel entre
    # processos (a iteracao de set acima e hash-randomizada; sem isto o ranking "pisca").
    ranked = sorted(combined.items(), key=lambda x: (-x[1], x[0]))
    results = []
    for key, score in ranked[:top_n]:
        if score > 0:
            meta = catalog.get(key, {})
            results.append((round(score), meta.get("name", key), meta))
    return results
