# -*- coding: utf-8 -*-
"""O teto de RATE_LIMIT_GLOBAL_MAX vale para o PROCESSO, nao por chave.

Regressao de um defeito real: o singleton era criado sem `global_max`, entao o
balde era por `thread_id`, que db.py resolve como o id de correlacao da request.
Atras de qualquer proxy que gere um id por requisicao, cada chamada ganhava balde
proprio e o limite nao limitava nada. SECURITY.md e o README sempre
descreveram um teto global; o codigo e' que havia divergido do proprio desenho
(a classe ja tinha o backstop, com o comentario "a rotacao de thread_id nao fura
este teto" — so nao era ligado).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from mcp_tiago_dados_abertos.execucao.rate_limiter import RateLimiter, limiter  # noqa: E402
from mcp_tiago_dados_abertos.infra.config import RATE_LIMIT_GLOBAL_MAX  # noqa: E402


def test_singleton_tem_teto_global_ligado():
    """O limiter do processo precisa do backstop; sem ele o teto e' por chave."""
    assert limiter._global_max == RATE_LIMIT_GLOBAL_MAX, (
        "singleton sem global_max: o teto vira por chave e a rotacao de id de "
        "correlacao fura o limite inteiro"
    )


def test_chave_rotativa_nao_fura_o_teto():
    """N+1 requests com chave SEMPRE diferente: a ultima tem que ser negada."""
    rl = RateLimiter(max_requests=10, window_seconds=60.0, burst=10_000, global_max=10)
    for i in range(10):
        ok, _ = rl.allow(f"corr-{i}")          # cada uma com id novo, como um proxy faria
        assert ok, f"request {i} negada antes do teto"
    ok, msg = rl.allow("corr-nova-em-folha")
    assert not ok, "chave nova furou o teto global"
    assert "global" in (msg or "").lower()


def test_teto_global_e_por_janela_nao_permanente():
    """O balde global expira: nao e' um teto vitalicio."""
    rl = RateLimiter(max_requests=5, window_seconds=0.3, burst=10_000, global_max=2)
    assert rl.allow("a")[0]
    assert rl.allow("b")[0]
    assert not rl.allow("c")[0]
    import time as _t
    _t.sleep(0.35)
    assert rl.allow("d")[0], "janela expirou e ainda bloqueia"
