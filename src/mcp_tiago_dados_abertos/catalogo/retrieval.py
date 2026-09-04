# -*- coding: utf-8 -*-
"""
Busca semantica leve via TF-IDF + cosine similarity.
Sem GPU, sem download de modelo, sem dependencias pesadas.
Usa sklearn (ja instalado) para indexar os contratos ODCS no startup.

Cada contrato vira um "documento" composto por:
  nome + tags + vectorTags + ragContext + typicalQuestions + aliases das colunas

A busca retorna os contratos mais semanticamente proximos da pergunta,
mesmo quando nao ha match exato de keywords.
"""

import logging
import threading
from typing import Optional

logger = logging.getLogger("mcp_tiago_dados_abertos.retrieval")

try:
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import cosine_similarity

    SKLEARN_OK = True
except ImportError:
    SKLEARN_OK = False
    logger.warning("sklearn nao instalado — busca semantica desabilitada.")


def _fingerprint(catalog: dict) -> int:
    """Identidade barata do corpus: quais datasets ele tem.

    O hash de str e cacheado pelo CPython, entao isto custa dezenas de
    microssegundos mesmo com centenas de contratos — irrelevante perto do
    fit_transform que ele evita repetir.
    """
    return hash((len(catalog), frozenset(catalog)))


class SemanticIndex:
    """Indice TF-IDF sobre os contratos ODCS."""

    def __init__(self):
        self._vectorizer: Optional[TfidfVectorizer] = None
        self._matrix = None
        self._names: list[str] = []
        self._catalog_ref: dict = {}
        self._fingerprint: Optional[int] = None

    def build(self, catalog: dict) -> None:
        """Constroi o indice a partir do catalogo ODCS. Chamado uma vez no startup."""
        if not SKLEARN_OK or not catalog:
            return

        self._catalog_ref = catalog
        self._fingerprint = _fingerprint(catalog)
        docs = []
        names = []

        for name, meta in catalog.items():
            if meta.get("status") == "discontinued":
                continue

            # Monta documento rico para cada contrato
            parts = [
                name.replace("-", " ").replace("_", " "),
                " ".join(meta.get("tags", [])),
                # vectorTags: sinonimos informais curados no contrato ("nivel da
                # agua", "energia limpa") — sem eles o TF-IDF so ve jargao ONS.
                " ".join(meta.get("vector_tags", [])),
                meta.get("rag_context", ""),
                meta.get("granularity", ""),
                meta.get("spatial_granularity", ""),
            ]

            # Adiciona typicalQuestions
            for q in meta.get("typical_questions", []):
                if isinstance(q, str):
                    parts.append(q)

            # Adiciona aliases das colunas (sinonimos)
            for col in meta.get("columns", []):
                parts.append(col.get("description", ""))
                for alias in col.get("aliases", []):
                    parts.append(alias)

            # Adiciona perguntas dos fewShotQueries
            for fs in meta.get("few_shot_queries", []):
                if isinstance(fs, dict):
                    parts.append(fs.get("question", ""))

            doc = " ".join(p for p in parts if p)
            docs.append(doc)
            names.append(name)

        if not docs:
            return

        # Constroi TF-IDF com ngrams para capturar termos compostos
        self._vectorizer = TfidfVectorizer(
            ngram_range=(1, 2),  # unigrams + bigrams
            max_features=5000,  # limita vocabulario
            stop_words=None,  # nao remove stopwords (portugues)
            strip_accents="unicode",  # 'geração' == 'geracao' (consistente com o ramo keyword)
            sublinear_tf=True,  # log(1 + tf) — melhor para textos curtos
            min_df=1,
            # max_df=0.95 exige >= 2 documentos (0.95 * 1 < min_df=1 -> ValueError do
            # sklearn). Corpus minusculo e o caso de quem esta COMECANDO um portal:
            # a busca nao pode explodir na cara dele. Acima de 20 docs o corte de
            # termo ubiquo volta a valer e o ranking do corpus real nao muda.
            max_df=1.0 if len(docs) < 20 else 0.95,
        )
        self._matrix = self._vectorizer.fit_transform(docs)
        self._names = names
        logger.info("Indice semantico: %d datasets, %d features.", len(names), self._matrix.shape[1])

    def is_ready(self) -> bool:
        return self._matrix is not None

    def matches(self, catalog: dict) -> bool:
        """O indice foi construido a partir DESTE catalogo?"""
        return self._fingerprint == _fingerprint(catalog)

    def search(self, query: str, top_n: int = 5) -> list[tuple[float, str, dict]]:
        """
        Busca semantica por similaridade de cosseno.
        Retorna [(score, nome, meta), ...] ordenado por relevancia.
        """
        if not SKLEARN_OK or self._matrix is None:
            return []

        query_vec = self._vectorizer.transform([query])
        scores = cosine_similarity(query_vec, self._matrix).flatten()

        # Ordena por score descendente
        ranked = sorted(
            zip(scores, self._names),
            key=lambda x: x[0],
            reverse=True,
        )

        results = []
        for score, name in ranked[:top_n]:
            if score > 0.01:  # threshold minimo
                meta = self._catalog_ref.get(name, {})
                results.append((round(float(score), 4), name, meta))

        return results


# Singleton
_index = SemanticIndex()
_index_lock = threading.Lock()


def build_index(catalog: dict) -> None:
    """Constroi o indice semantico (ex.: testes). Sobrescreve indice existente."""
    with _index_lock:
        _index.build(catalog)


def ensure_semantic_index(catalog: dict) -> None:
    """Garante indice TF-IDF antes de buscas; adia trabalho pesado do import do modulo.

    Reconstroi quando o catalogo muda: em prod existe um corpus so (constroi uma
    vez e nunca mais), mas um processo que carregasse dois catalogos ranquearia
    contra o errado, calado.
    """
    if not SKLEARN_OK or not catalog:
        return
    if _index.is_ready() and _index.matches(catalog):
        return
    with _index_lock:
        if _index.is_ready() and _index.matches(catalog):
            return
        _index.build(catalog)


def semantic_search(query: str, top_n: int = 5) -> list[tuple[float, str, dict]]:
    """Busca semantica. Retorna [(score, nome, meta), ...]."""
    return _index.search(query, top_n)
