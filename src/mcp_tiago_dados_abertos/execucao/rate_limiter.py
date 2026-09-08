# -*- coding: utf-8 -*-
"""
Rate limiter para proteger o S3 do ONS.

Este e' o unico anti-abuso do servidor; limite por origem e' trabalho do proxy
na frente. O rate limiter local serve
como backstop e para logging de metricas.

Defesas:
  - teto GLOBAL agregado;
  - lock (thread-safe sob asyncio.to_thread);
  - teto de cardinalidade de chaves + truncagem de chave gigante (anti-OOM).

Sem dependencias externas — dict em memoria.
"""

import threading
import time
from typing import Optional

from mcp_tiago_dados_abertos.infra.config import RATE_LIMIT_GLOBAL_MAX, RATE_LIMIT_MAX_KEYS

# Higiene de chave (anti-OOM): teto fixo generico, independente da origem da chave.
_MAX_KEY_LEN = 256


class RateLimiter:
    """Token bucket por chave + teto global agregado. Thread-safe."""

    def __init__(
        self,
        max_requests: int = 30,
        window_seconds: float = 60.0,
        burst: int = 5,
        max_keys: int = RATE_LIMIT_MAX_KEYS,
        global_max: Optional[int] = None,
    ):
        self._max = max_requests
        self._window = window_seconds
        self._burst = burst
        self._max_keys = max_keys
        self._global_max = global_max
        self._buckets: dict[str, list[float]] = {}
        self._global: list[float] = []
        self._lock = threading.Lock()

    @staticmethod
    def _norm(key: str) -> str:
        # Trunca chaves gigantes para evitar OOM.
        return (key or "")[:_MAX_KEY_LEN]

    def allow(self, key: str) -> tuple[bool, Optional[str]]:
        """Verifica se a request e permitida. Retorna (permitido, mensagem_de_erro)."""
        key = self._norm(key)
        with self._lock:
            now = time.monotonic()

            # Backstop global — a rotacao de thread_id nao fura este teto.
            if self._global_max is not None:
                self._global = [t for t in self._global if now - t < self._window]
                if len(self._global) >= self._global_max:
                    wait = self._window - (now - self._global[0])
                    return False, (
                        f"[RATE LIMIT] Teto global de {self._global_max} queries por "
                        f"{self._window:.0f}s atingido. Aguarde {wait:.0f}s."
                    )

            # Por chave — evicta a mais antiga se estourar o teto de cardinalidade (anti-OOM).
            if key not in self._buckets:
                while len(self._buckets) >= self._max_keys:
                    self._buckets.pop(next(iter(self._buckets)))
                self._buckets[key] = []

            self._buckets[key] = [t for t in self._buckets[key] if now - t < self._window]

            if len(self._buckets[key]) >= self._max:
                wait = self._window - (now - self._buckets[key][0])
                return False, (
                    f"[RATE LIMIT] Limite de {self._max} queries por {self._window:.0f}s atingido. Aguarde {wait:.0f}s."
                )

            recent = [t for t in self._buckets[key] if now - t < 1.0]
            if len(recent) >= self._burst:
                return False, (f"[RATE LIMIT] Burst de {self._burst} queries/segundo atingido. Aguarde 1 segundo.")

            # Permite e registra (por chave e no agregado global).
            self._buckets[key].append(now)
            if self._global_max is not None:
                self._global.append(now)
            return True, None

    def reset(self, key: str) -> None:
        """Reseta o bucket de uma chave."""
        with self._lock:
            self._buckets.pop(self._norm(key), None)

    @property
    def active_keys(self) -> int:
        """Numero de chaves ativas."""
        return len(self._buckets)


# Singleton — backstop do PROCESSO. `global_max` e o que garante o teto: db.py
# chama allow() com o id de correlacao da request, que muda a cada chamada atras
# de um proxy; sem o teto global, cada request ganharia balde proprio e o limite
# nao limitaria nada. O balde por chave continua valendo por cima dele.
# Nao ha limite por cliente: o servidor nao distingue origem — isso e' trabalho
# de um proxy/gateway na frente (ver README.md, secao Configuracao).
limiter = RateLimiter(
    max_requests=RATE_LIMIT_GLOBAL_MAX,  # por chave (id de correlacao)
    window_seconds=60.0,
    burst=20,  # amortece rajada: max 20 queries por segundo no processo
    global_max=RATE_LIMIT_GLOBAL_MAX,  # teto agregado do processo (default 600)
)
