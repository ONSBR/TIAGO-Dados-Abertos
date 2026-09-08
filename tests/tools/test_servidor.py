"""Testes do servidor — catalogo, busca, motor e validador."""


import pytest

from mcp_tiago_dados_abertos.catalogo.contracts import find, format_schema, load_all_contracts, rank_datasets
from mcp_tiago_dados_abertos.catalogo.retrieval import build_index, semantic_search
from mcp_tiago_dados_abertos.execucao.validator import SqlValidator


@pytest.fixture(scope="session")
def catalog():
    cat = load_all_contracts()
    build_index(cat)
    return cat


# ── Catalogo ──────────────────────────────────────────────────────────────────


def test_config():
    from mcp_tiago_dados_abertos.infra.config import CONTRACTS_DIR, PORT, VERSION

    assert CONTRACTS_DIR.exists(), f"Pasta nao existe: {CONTRACTS_DIR}"
    assert PORT == 8003
    assert VERSION


def test_catalog_ons_count(catalog):
    ons = [k for k, v in catalog.items() if v["orgao"] == "ons"]
    assert len(ons) == 78, f"Esperado 78 ONS, got {len(ons)}"


def test_catalog_so_tem_ons(catalog):
    """O catalogo e so ONS. Um contrato fora de contracts/ons entrando por engano muda o
    IDF do indice e o roteamento."""
    orgaos = {v["orgao"] for v in catalog.values()}
    assert orgaos == {"ons"}, f"catalogo com orgao fora de ons: {sorted(orgaos)}"


def test_find_and_fuzzy(catalog):
    assert find(catalog, "carga-energia") is not None
    assert find(catalog, "CARGA-ENERGIA") is not None  # case-insensitive
    assert find(catalog, "carga_energia") is not None  # separador


def test_format_schema(catalog):
    schema = format_schema(find(catalog, "carga-energia"))
    assert "columns:" in schema
    assert "val_cargaenergiamwmed" in schema


def test_all_contracts_have_semantics(catalog):
    sem = [k for k, v in catalog.items() if v.get("semantics")]
    assert len(sem) == len(catalog), f"Esperado semantics em todos, got {len(sem)}/{len(catalog)}"


def test_nested_semantics_envelope_unwrapped(catalog):
    # Alguns contratos embrulham em customProperties.semantics.semantics;
    # o catalogo deve desembrulhar e expor metrics no nivel real.
    meta = find(catalog, "balanco-energia-subsistema")
    sem = meta.get("semantics") or {}
    assert "semantics" not in sem, "envelope aninhado nao foi desembrulhado"
    assert sem.get("metrics"), "metrics deveria estar acessivel apos desembrulhar"


def test_few_datasets_without_metrics(catalog):
    # Cadastros podem legitimamente nao declarar metricas agregaveis.
    sem_metric = [
        v["name"]
        for v in catalog.values()
        if v.get("orgao") == "ons" and not (v.get("semantics") or {}).get("metrics")
    ]
    assert len(sem_metric) <= 6, f"Esperado poucos sem metrics, got {len(sem_metric)}: {sem_metric}"


# ── Busca semantica ───────────────────────────────────────────────────────────


def test_semantic_search(catalog):
    results = semantic_search("carga de energia subsistema", top_n=5)
    assert results
    assert any("carga" in name for _, name, _ in results)


def test_rank_curtailment(catalog):
    results = rank_datasets(catalog, "curtailment eolico restricao", top_n=3)
    assert results
    assert any("eolic" in name for _, name, _ in results)


def test_rank_routing_boost_ranking(catalog):
    # geracao-usina-2 suporta ranking; deve aparecer p/ pergunta de ranking de usinas
    results = rank_datasets(catalog, "ranking das maiores usinas por geracao", top_n=5)
    names = [n for _, n, _ in results]
    assert any("usina" in n for n in names)


# ── Motor semantico ───────────────────────────────────────────────────────────


# ── Validador (seguranca read-only) ───────────────────────────────────────────


@pytest.fixture
def validator():
    return SqlValidator(con=None)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM read_parquet('s3://ons-aws-prod-opendata/dataset/x.parquet') LIMIT 1",
        "WITH t AS (SELECT 1 AS a) SELECT a FROM t",
    ],
)
def test_validator_permite_select(validator, sql):
    """Valida que SQL legitimo com buckets ONS passa apos hardening."""
    ok, _ = validator.is_safe(sql)
    assert ok


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE x",
        "DELETE FROM x",
        "COPY (SELECT 1) TO 'out.csv'",
        "ATTACH 'evil.db' AS e",
        "INSTALL httpfs",
        "PRAGMA database_list",
        "SELECT * FROM read_parquet('/etc/passwd')",
        "SELECT * FROM read_csv('local.csv')",
    ],
)
def test_validator_bloqueia_perigoso(validator, sql):
    ok, _ = validator.is_safe(sql)
    assert not ok, f"deveria bloquear: {sql}"


def test_validator_sem_falso_positivo_payload(validator):
    # 'payload' contem 'load' mas nao deve ser bloqueado (apos hardening, bucket ONS obrigatorio)
    ok, _ = validator.is_safe("SELECT payload FROM read_parquet('s3://ons-aws-prod-opendata/dataset/x.parquet')")
    assert ok


if __name__ == "__main__":
    import pytest as _p

    _p.main([__file__, "-v", "--tb=short"])
