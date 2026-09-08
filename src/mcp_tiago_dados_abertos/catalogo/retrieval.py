# -*- coding: utf-8 -*-
"""
Busca lexical por BM25 sobre os contratos ODCS. Biblioteca padrao, sem modelo.

Cada contrato vira um "documento": nome, tags, vectorTags, aiContext, granularidade,
perguntas tipicas, descricoes e aliases de coluna e perguntas dos exemplos. A busca
devolve os contratos com maior escore BM25 para a pergunta, normalizado para 0..1
(o primeiro colocado vale 1.0), no mesmo formato que o ranking consome.

Por que BM25 e nao TF-IDF: medido em 05/09/2026 em 44 perguntas reais que nunca
entraram em calibracao, o BM25 acertou 61-66% no top-1 contra 48-52% do TF-IDF
calibrado, em catalogos de 78 e de 400 contratos, com o mesmo top-3. Sem o
scikit-learn saem 211 MB da imagem. Parametros k1=1.5 e b=0.4 vieram do ajuste no
conjunto de treino; o padrao (1.5/0.75) ja superava o TF-IDF.
"""

import logging
import math
import re
import threading
import unicodedata
from collections import Counter

logger = logging.getLogger("mcp_tiago_dados_abertos.retrieval")

BM25_K1 = 1.5
BM25_B = 0.4
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokens(texto: str) -> list[str]:
    """Minusculas sem acento, tokens alfanumericos de 2+ caracteres, mais bigramas."""
    s = unicodedata.normalize("NFKD", (texto or "").lower())
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    uni = [t for t in _TOKEN_RE.findall(s) if len(t) >= 2]
    return uni + [a + "_" + b for a, b in zip(uni, uni[1:])]


def _documento(name: str, meta: dict) -> str:
    partes = [
        name.replace("-", " ").replace("_", " "),
        " ".join(meta.get("tags", [])),
        " ".join(meta.get("vector_tags", [])),
        meta.get("rag_context", ""),
        meta.get("granularity", ""),
        meta.get("spatial_granularity", ""),
    ]
    partes += [q for q in meta.get("typical_questions", []) if isinstance(q, str)]
    for col in meta.get("columns", []):
        partes.append(col.get("description", ""))
        partes += [a for a in col.get("aliases", []) if isinstance(a, str)]
    partes += [fs.get("question", "") for fs in meta.get("few_shot_queries", []) if isinstance(fs, dict)]
    return " ".join(p for p in partes if p)


def _fingerprint(catalog: dict) -> int:
    """Identidade barata do corpus: quais datasets ele tem."""
    return hash((len(catalog), frozenset(catalog)))


class SemanticIndex:
    """Indice BM25 sobre os contratos ODCS."""

    def __init__(self):
        self._tf: dict[str, Counter] = {}
        self._dl: dict[str, int] = {}
        self._idf: dict[str, float] = {}
        self._avgdl: float = 1.0
        self._catalog_ref: dict = {}
        self._fingerprint: int | None = None

    def build(self, catalog: dict) -> None:
        """Constroi o indice a partir do catalogo. Milissegundos para centenas de contratos."""
        if not catalog:
            return
        tf: dict[str, Counter] = {}
        for name, meta in catalog.items():
            if meta.get("status") == "discontinued":
                continue
            tf[name] = Counter(_tokens(_documento(name, meta)))
        if not tf:
            return
        n_docs = len(tf)
        df: Counter = Counter()
        for c in tf.values():
            df.update(c.keys())
        self._tf = tf
        self._dl = {name: sum(c.values()) for name, c in tf.items()}
        self._avgdl = sum(self._dl.values()) / n_docs
        self._idf = {t: math.log(1 + (n_docs - n + 0.5) / (n + 0.5)) for t, n in df.items()}
        self._catalog_ref = catalog
        self._fingerprint = _fingerprint(catalog)
        logger.info("Indice BM25: %d datasets, %d termos.", n_docs, len(self._idf))

    def is_ready(self) -> bool:
        return bool(self._tf)

    def matches(self, catalog: dict) -> bool:
        """O indice foi construido a partir DESTE catalogo?"""
        return self._fingerprint == _fingerprint(catalog)

    def search(self, query: str, top_n: int = 5) -> list[tuple[float, str, dict]]:
        """Retorna [(escore 0..1, nome, meta), ...] em ordem decrescente; vazio se nada casa."""
        if not self._tf:
            return []
        q = _tokens(query)
        brutos = []
        for name, tf in self._tf.items():
            norm = BM25_K1 * (1 - BM25_B + BM25_B * self._dl[name] / self._avgdl)
            s = 0.0
            for t in q:
                f = tf.get(t)
                if f:
                    s += self._idf[t] * f * (BM25_K1 + 1) / (f + norm)
            if s > 0:
                brutos.append((s, name))
        if not brutos:
            return []
        brutos.sort(key=lambda x: (-x[0], x[1]))
        maximo = brutos[0][0]
        return [(round(s / maximo, 4), name, self._catalog_ref.get(name, {})) for s, name in brutos[:top_n]]


# Singleton
_index = SemanticIndex()
_index_lock = threading.Lock()


def build_index(catalog: dict) -> None:
    """Constroi o indice (ex.: testes). Sobrescreve indice existente."""
    with _index_lock:
        _index.build(catalog)


def ensure_semantic_index(catalog: dict) -> None:
    """Garante o indice antes de buscas. Reconstroi quando o catalogo muda: em prod existe
    um corpus so, mas um processo que carregasse dois catalogos ranquearia contra o errado."""
    if not catalog:
        return
    if _index.is_ready() and _index.matches(catalog):
        return
    with _index_lock:
        if _index.is_ready() and _index.matches(catalog):
            return
        _index.build(catalog)


def semantic_search(query: str, top_n: int = 5) -> list[tuple[float, str, dict]]:
    """Busca lexical BM25. Retorna [(escore 0..1, nome, meta), ...]."""
    return _index.search(query, top_n)
