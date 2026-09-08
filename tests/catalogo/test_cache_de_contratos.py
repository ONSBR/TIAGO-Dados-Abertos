# -*- coding: utf-8 -*-
"""Cache pre-parseado dos contratos (contracts_cache.json): validacao e carga.

O cache e' artefato (scripts/build_contracts_cache.py) e so' substitui o parse dos
YAML quando e' INTEGRO: version presente, docs nao-vazio, n_docs coerente, hash
confere. Cache parcial ou truncado cai para os YAML, nunca e' servido calado.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from mcp_tiago_dados_abertos.catalogo.contracts import _cache_valido


@pytest.fixture
def workspace_tmp_path():
    """Diretório temporário gravável quando o sandbox bloqueia o %TEMP%."""
    with TemporaryDirectory(prefix="test-contracts-s3-", dir=Path.cwd()) as temp_dir:
        yield Path(temp_dir)

import hashlib
import json


def _build_cache_json(docs: list[dict], include_n_docs: bool = True, include_hash: bool = True) -> bytes:
    """Helper: monta contracts_cache.json valido (ou parcial, conforme flags)."""
    payload = {"version": 1, "docs": docs}
    if include_n_docs:
        payload["n_docs"] = len(docs)
    if include_hash:
        content = json.dumps(docs, sort_keys=True, ensure_ascii=False)
        payload["content_hash"] = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")

class TestCacheValido:
    """Unit tests para _cache_valido (Mudanca 2)."""

    def test_cache_valido_ok(self, workspace_tmp_path):
        """Cache com version+n_docs+docs coerentes = valido."""
        docs = [{"orgao": "ons", "stem": "carga", "data": {"id": 1}}]
        cache_path = workspace_tmp_path / "contracts_cache.json"
        cache_path.write_bytes(_build_cache_json(docs))
        assert _cache_valido(cache_path) is True

    def test_cache_n_docs_errado(self, workspace_tmp_path):
        """n_docs != len(docs) = invalido."""
        docs = [{"orgao": "ons", "stem": "carga", "data": {"id": 1}}]
        cache_path = workspace_tmp_path / "contracts_cache.json"
        content = json.loads(_build_cache_json(docs).decode())
        content["n_docs"] = 999  # errado
        cache_path.write_text(json.dumps(content), encoding="utf-8")
        assert _cache_valido(cache_path) is False

    def test_cache_hash_divergente(self, workspace_tmp_path):
        """content_hash presente mas diverge = invalido."""
        docs = [{"orgao": "ons", "stem": "carga", "data": {"id": 1}}]
        cache_path = workspace_tmp_path / "contracts_cache.json"
        content = json.loads(_build_cache_json(docs).decode())
        content["content_hash"] = "0" * 64  # hash errado
        cache_path.write_text(json.dumps(content), encoding="utf-8")
        assert _cache_valido(cache_path) is False

    def test_cache_json_corrompido(self, workspace_tmp_path):
        """JSON invalido = False (nunca propaga excecao)."""
        cache_path = workspace_tmp_path / "contracts_cache.json"
        cache_path.write_bytes(b"{not valid json")
        assert _cache_valido(cache_path) is False

    def test_cache_legado_sem_n_docs_hash_e_valido(self, workspace_tmp_path):
        """Cache legado (sem n_docs/hash, apenas version+docs) = valido (retrocompat)."""
        docs = [{"orgao": "ons", "stem": "carga", "data": {"id": 1}}]
        cache_path = workspace_tmp_path / "contracts_cache.json"
        cache_path.write_bytes(_build_cache_json(docs, include_n_docs=False, include_hash=False))
        assert _cache_valido(cache_path) is True

    def test_cache_docs_vazio_e_invalido(self, workspace_tmp_path):
        """docs vazio = invalido (mesmo com version)."""
        cache_path = workspace_tmp_path / "contracts_cache.json"
        cache_path.write_bytes(_build_cache_json([]))
        assert _cache_valido(cache_path) is False

    def test_cache_sem_version_e_invalido(self, workspace_tmp_path):
        """Sem version = invalido."""
        cache_path = workspace_tmp_path / "contracts_cache.json"
        content = {"docs": [{"orgao": "ons", "stem": "a", "data": {}}]}
        cache_path.write_text(json.dumps(content), encoding="utf-8")
        assert _cache_valido(cache_path) is False

class TestCatalogGuard:
    """Teste da rede de seguranca no contracts.py (Mudanca 2b).

    Nota: estes testes precisam ser isolados porque manipulam o estado global
    do modulo catalog. Usamos autouse fixture para restaurar o estado.
    """

    @pytest.fixture(autouse=True)
    def _isolate_catalog_module(self):
        """Salva e restaura o estado do modulo catalog."""
        import importlib

        from mcp_tiago_dados_abertos.catalogo import contracts
        from mcp_tiago_dados_abertos.infra import config

        # Salva o CONTRACTS_DIR original
        original_contracts_dir = config.CONTRACTS_DIR
        yield
        # Restaura o CONTRACTS_DIR original e recarrega os modulos
        # O catalog importa CONTRACTS_DIR direto do config, entao precisa
        # recarregar config primeiro, depois catalog.
        config.CONTRACTS_DIR = original_contracts_dir
        importlib.reload(config)
        importlib.reload(contracts)

    def test_catalog_rejeita_cache_parcial(self, workspace_tmp_path, monkeypatch):
        """contracts.load_all_contracts com cache parcial (n_docs errado) nao serve docs truncados."""
        import importlib

        # Monta cache com n_docs errado
        docs = [{"orgao": "ons", "stem": "carga-energia", "data": {"name": "carga-energia"}}]
        cache_content = json.loads(_build_cache_json(docs).decode())
        cache_content["n_docs"] = 999  # parcial/truncado

        contracts_dir = workspace_tmp_path / "contracts"
        contracts_dir.mkdir()
        cache_path = contracts_dir / "contracts_cache.json"
        cache_path.write_text(json.dumps(cache_content), encoding="utf-8")

        # Sem YAMLs de fallback -> catalogo deve ficar vazio (seguro)
        from mcp_tiago_dados_abertos.catalogo import contracts
        from mcp_tiago_dados_abertos.infra import config

        monkeypatch.setattr(config, "CONTRACTS_DIR", contracts_dir)
        # Recarrega catalog para pegar o novo CONTRACTS_DIR
        importlib.reload(contracts)

        result = contracts.load_all_contracts()

        # NAO deve ter servido o cache parcial
        assert len(result) == 0, f"deveria estar vazio, encontrou: {list(result.keys())}"

    def test_catalog_aceita_cache_valido(self, workspace_tmp_path, monkeypatch):
        """contracts.load_all_contracts com cache valido carrega os docs."""
        import importlib

        docs = [{"orgao": "ons", "stem": "carga-energia", "data": {"name": "carga-energia"}}]
        cache_bytes = _build_cache_json(docs)

        contracts_dir = workspace_tmp_path / "contracts"
        contracts_dir.mkdir()
        cache_path = contracts_dir / "contracts_cache.json"
        cache_path.write_bytes(cache_bytes)

        from mcp_tiago_dados_abertos.catalogo import contracts
        from mcp_tiago_dados_abertos.infra import config

        monkeypatch.setattr(config, "CONTRACTS_DIR", contracts_dir)
        # Recarrega catalog para pegar o novo CONTRACTS_DIR
        importlib.reload(contracts)

        result = contracts.load_all_contracts()

        assert len(result) == 1
        assert "ons/carga-energia" in result

import datetime


def _encode(o):
    """Replica a receita de encode do build_contracts_cache.py."""
    if isinstance(o, datetime.datetime):
        return {"__datetime__": o.isoformat()}
    if isinstance(o, datetime.date):
        return {"__date__": o.isoformat()}
    raise TypeError(f"tipo nao serializavel no cache: {type(o)}")

def _build_cache_json_with_dates(docs: list[dict]) -> bytes:
    """Monta cache com datas usando a receita EXATA do builder."""
    content_for_hash = json.dumps(docs, sort_keys=True, ensure_ascii=False, default=_encode)
    content_hash = hashlib.sha256(content_for_hash.encode("utf-8")).hexdigest()
    payload = {
        "version": 1,
        "n_docs": len(docs),
        "content_hash": content_hash,
        "docs": docs,
    }
    return json.dumps(payload, ensure_ascii=False, default=_encode).encode("utf-8")

class TestCacheComDatas:
    """FIX 4: Testes de regressao com datetime.date/datetime nos docs."""

    @pytest.fixture(autouse=True)
    def _isolate_catalog_module(self):
        """Salva e restaura o estado do modulo catalog."""
        import importlib

        from mcp_tiago_dados_abertos.catalogo import contracts
        from mcp_tiago_dados_abertos.infra import config

        original_contracts_dir = config.CONTRACTS_DIR
        yield
        config.CONTRACTS_DIR = original_contracts_dir
        importlib.reload(config)
        importlib.reload(contracts)

    def test_cache_valido_aceita_datas(self, workspace_tmp_path):
        """_cache_valido retorna True para cache com datas construido pela receita do builder."""
        docs = [
            {
                "orgao": "ons",
                "stem": "carga-energia",
                "data": {
                    "name": "carga-energia",
                    # Simula datas como aparecem no corpus real apos parse YAML
                    "temporalCoverageStart": datetime.date(2020, 1, 1),
                    "temporalCoverageEnd": datetime.date(2024, 12, 31),
                },
            },
            {
                "orgao": "ons",
                "stem": "geracao-usina",
                "data": {
                    "name": "geracao-usina",
                    "ultimaAtualizacao": datetime.datetime(2024, 6, 15, 10, 30, 0),
                },
            },
        ]
        cache_path = workspace_tmp_path / "contracts_cache.json"
        cache_path.write_bytes(_build_cache_json_with_dates(docs))
        assert _cache_valido(cache_path) is True

    def test_catalog_serve_cache_com_datas(self, workspace_tmp_path, monkeypatch):
        """Catalog carrega cache com datas e retorna objetos date no 'data'."""
        import importlib

        docs = [
            {
                "orgao": "ons",
                "stem": "carga-energia",
                "data": {
                    "name": "carga-energia",
                    "customProperties": [
                        {"property": "temporalCoverageStart", "value": datetime.date(2020, 1, 1)},
                    ],
                },
            },
        ]

        contracts_dir = workspace_tmp_path / "contracts"
        contracts_dir.mkdir()
        cache_path = contracts_dir / "contracts_cache.json"
        cache_path.write_bytes(_build_cache_json_with_dates(docs))

        from mcp_tiago_dados_abertos.catalogo import contracts
        from mcp_tiago_dados_abertos.infra import config

        monkeypatch.setattr(config, "CONTRACTS_DIR", contracts_dir)
        importlib.reload(contracts)

        result = contracts.load_all_contracts()

        assert len(result) == 1, f"deveria ter 1 doc, encontrou: {len(result)}"
        assert "ons/carga-energia" in result
        # A data deve ter sido decodificada como datetime.date
        meta = result["ons/carga-energia"]
        # O valor esta em period_start (extraido de customProperties)
        assert meta["period_start"] == datetime.date(2020, 1, 1), f"got {meta['period_start']!r}"

    def test_builder_gera_cache_que_passa_validacao(self, workspace_tmp_path, monkeypatch):
        """Teste ponta-a-ponta: build_contracts_cache.main() gera cache que _cache_valido aprova."""
        import sys

        # Cria mini-corpus YAML com data
        contracts_dir = workspace_tmp_path / "contracts"
        ons_dir = contracts_dir / "ons"
        ons_dir.mkdir(parents=True)

        yaml_content = """\
name: mini-teste
customProperties:
  - property: temporalCoverageStart
    value: 2020-01-01
schema: []
"""
        (ons_dir / "mini-teste.odcs.yaml").write_text(yaml_content, encoding="utf-8")

        # Executa o builder como subprocess (scripts/ nao e pacote importavel)
        mcp_root = Path(__file__).resolve().parents[2]
        builder_script = mcp_root / "scripts" / "build_contracts_cache.py"
        result = subprocess.run(
            [sys.executable, str(builder_script), f"--contracts-dir={contracts_dir}"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"builder falhou: {result.stderr}"

        # Verifica que o cache foi gerado e e valido
        cache_path = contracts_dir / "contracts_cache.json"
        assert cache_path.exists()
        assert _cache_valido(cache_path) is True


def test_manifesto_do_cache_sobrevive_a_mudanca_de_mtime(tmp_path):
    """O manifesto e por conteudo. Um cache construido no build da imagem continua valido
    depois que o Docker trunca o mtime dos YAML para segundos inteiros ao exportar a camada;
    com o manifesto antigo, por mtime_ns, o cache era rejeitado em toda carga."""
    import os
    import time

    from mcp_tiago_dados_abertos.catalogo import contracts as C

    base = tmp_path / "contracts"
    (base / "ons").mkdir(parents=True)
    y = base / "ons" / "a.odcs.yaml"
    y.write_text("apiVersion: v3.1.0\nkind: DataContract\nid: a\n", encoding="utf-8")
    docs = [{"orgao": "ons", "stem": "a", "data": {"id": "a"}}]
    conteudo = json.dumps(docs, sort_keys=True, ensure_ascii=False)
    payload = {
        "version": 1, "n_docs": 1, "docs": docs,
        "content_hash": hashlib.sha256(conteudo.encode("utf-8")).hexdigest(),
        "yaml_manifest": C.yaml_manifest(base),
    }
    cache = base / "contracts_cache.json"
    cache.write_text(json.dumps(payload), encoding="utf-8")
    assert C._cache_valido(cache)

    # mtime muda (truncado para segundos, como o Docker faz), conteudo nao: continua valido
    t = int(time.time()) - 1000
    os.utime(y, (t, t))
    assert C._cache_valido(cache)

    # conteudo muda: invalida
    y.write_text("apiVersion: v3.1.0\nkind: DataContract\nid: b\n", encoding="utf-8")
    assert not C._cache_valido(cache)
