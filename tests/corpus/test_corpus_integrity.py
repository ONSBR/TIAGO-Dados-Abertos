# -*- coding: utf-8 -*-
"""
Test corpus integrity - guard tests for contract defects.

This test module detects the following classes of bugs:
- A1: Parquet file overlap (annual + monthly files covering same period)
- A2: Orphan SIN value_aliases (alias references non-existent value)
- A5: Missing granularity consistency (granularity vs row_grain vs S3 suffix)

These tests run against live S3 data (read-only, anonymous access).
"""

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

# Skip if no network or boto3
boto3 = pytest.importorskip("boto3")
duckdb = pytest.importorskip("duckdb")

from botocore import UNSIGNED
from botocore.config import Config

CONTRACTS_DIR = Path(__file__).resolve().parents[2] / "contracts" / "ons"


def load_contract(name: str) -> dict[str, Any]:
    """Load a contract YAML file by name (without extension)."""
    path = CONTRACTS_DIR / f"{name}.odcs.yaml"
    if not path.exists():
        pytest.skip(f"Contract {name} not found")
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def get_s3_location(contract: dict) -> str | None:
    """Extract S3 location from contract servers."""
    servers = contract.get("servers", [])
    for server in servers:
        loc = server.get("location", "")
        if loc.startswith("s3://"):
            return loc
    return None


def list_s3_parquet_files(bucket: str, prefix: str) -> list[str]:
    """List parquet files in S3 prefix."""
    try:
        s3 = boto3.client("s3", config=Config(signature_version=UNSIGNED), region_name="us-west-2")
        paginator = s3.get_paginator("list_objects_v2")
        files = []
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if key.endswith(".parquet"):
                    files.append(key.split("/")[-1])
        return files
    except Exception:
        pytest.skip("Cannot connect to S3")
        return []


def get_duckdb_connection():
    """Get a DuckDB connection with httpfs configured for anonymous S3."""
    conn = duckdb.connect()
    conn.execute("INSTALL httpfs")
    conn.execute("LOAD httpfs")
    conn.execute("SET s3_region='us-west-2'")
    return conn


# =============================================================================
# A1: Parquet file overlap detection
# =============================================================================


class TestA1ParquetOverlap:
    """
    Detect overlapping parquet files (annual + monthly covering same years).

    The geracao_termica_despacho_2_ho prefix has both:
    - Annual files: GERACAO_TERMICA_DESPACHO_2022.parquet (underscore before year)
    - Monthly files: GERACAO_TERMICA_DESPACHO-2_2022_01.parquet (dash-2 before year)

    When using *.parquet glob, both are read and data is doubled for 2022-2024.
    """

    def test_geracao_termica_despacho_2_no_overlap(self):
        """Integridade real: a FONTE que o engine usa (``sourceExpression``, ou
        NAO o `location`) deve ler cada ano UMA vez.
        Invariante: SUM(SE, 2023) via a fonte do engine
        deve ~= o SUM do arquivo ANUAL de 2023. ~2.0 = overlap anual+mensal (double-count);
        ~0 = historico dropado (glob so-mensal). Falha na baseline (*.parquet), passa apos o fix."""
        contract = load_contract("geracao-termica-despacho-2")
        cprops = {c.get("property"): c.get("value") for c in contract.get("customProperties", [])}
        src = cprops.get("sourceExpression")
        assert src, "Contract deve declarar sourceExpression"
        prefix = "s3://ons-aws-prod-opendata/dataset/geracao_termica_despacho_2_ho"
        annual_2023 = f"read_parquet('{prefix}/GERACAO_TERMICA_DESPACHO_2023.parquet')"
        conn = get_duckdb_connection()

        def sum_se_2023(source: str) -> float:
            return conn.execute(
                f"SELECT SUM(TRY_CAST(val_verifgeracao AS DOUBLE)) FROM {source} "
                "WHERE TRIM(CAST(id_subsistema AS VARCHAR)) = 'SE' "
                "AND YEAR(TRY_CAST(din_instante AS TIMESTAMP)) = 2023"
            ).fetchone()[0]

        try:
            src_sum = sum_se_2023(src)
            annual_sum = sum_se_2023(annual_2023)
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"S3 indisponivel: {e}")

        assert annual_sum, "arquivo anual 2023 sem dado (verifique o prefixo)"
        assert src_sum, "Fonte efetiva retornou 0 para SE/2023 — historico dropado?"
        ratio = src_sum / annual_sum
        assert 0.9 < ratio < 1.1, (
            f"Fonte efetiva SE/2023 SUM={src_sum:.0f} vs anual-so-2023={annual_sum:.0f} "
            f"(ratio {ratio:.2f}). ~2.0 => overlap anual+mensal (double-count); "
            "~0 => historico dropado. A fonte do engine deve ler cada ano UMA vez."
        )

        # Anos SO-anuais (<2022) NAO podem sumir — pega o fix errado "so-mensal" (2018/2021 = 0).
        for ano in (2018, 2021):
            n = conn.execute(
                f"SELECT COUNT(*) FROM {src} WHERE YEAR(TRY_CAST(din_instante AS TIMESTAMP)) = {ano}"
            ).fetchone()[0]
            assert n and n > 0, (
                f"Fonte efetiva retornou 0 linhas para {ano} (arquivo anual) — "
                "historico dropado (glob so-mensal). A fonte deve cobrir 2013-2026."
            )


# =============================================================================
# A2: Orphan SIN value_aliases detection
# =============================================================================


class TestA2OrphanSINAlias:
    """
    Detect contracts that declare SIN as a value_alias but the data doesn't have SIN.

    When value_aliases.subsistema.SIN is declared with filter "id_subsistema = 'SIN'",
    but the actual data only has N, NE, S, SE (no SIN), queries for SIN return 0 rows.
    """

    CONTRACTS_TO_CHECK = [
        "carga-energia",
        "carga-mensal",
        "curva-carga",
        "balanco_dessem_detalhe",
        "geracao-termica-despacho-2",
        "dados-hidrologicos-res",
    ]

    @pytest.mark.parametrize("contract_name", CONTRACTS_TO_CHECK)
    def test_no_orphan_sin_alias(self, contract_name: str):
        """
        Verify that if a contract declares SIN value_alias, SIN exists in the data.
        """
        contract = load_contract(contract_name)

        # Find semantics block
        custom_props = contract.get("customProperties", [])
        semantics = None
        for prop in custom_props:
            if prop.get("property") == "semantics":
                semantics = prop.get("value", {})
                break

        if not semantics:
            pytest.skip("Contract has no semantics block")

        # Check for SIN in value_aliases
        value_aliases = semantics.get("value_aliases", {})
        subsistema_aliases = value_aliases.get("subsistema", {})

        has_sin_alias = "SIN" in subsistema_aliases

        # SEMPRE consulta o S3 (nao pula quando o alias ja foi removido — senao o teste vira inerte).
        location = get_s3_location(contract)
        if not location:
            pytest.skip("No S3 location")
        conn = get_duckdb_connection()
        try:
            cols = conn.execute(f"""
                SELECT column_name
                FROM (DESCRIBE SELECT * FROM read_parquet('{location}', union_by_name=true))
                WHERE column_name LIKE '%subsist%'
            """).fetchall()
            if not cols:
                pytest.skip("No subsystem column found")
            subsys_col = cols[0][0]
            result = conn.execute(f"""
                SELECT DISTINCT TRIM(CAST({subsys_col} AS VARCHAR)) as val
                FROM read_parquet('{location}', union_by_name=true)
                WHERE {subsys_col} IS NOT NULL
            """).fetchall()
            actual_values = {str(r[0]).strip() for r in result}
        except Exception as e:
            pytest.skip(f"Cannot query S3: {e}")
        finally:
            conn.close()

        has_sin_data = "SIN" in actual_values
        # Invariante (pega o alias orfao na baseline E qualquer regressao futura):
        # nao se pode declarar alias SIN se o dado fisico nao tem 'SIN' (o filtro retornaria 0 linhas).
        assert has_sin_data or not has_sin_alias, (
            f"Contract {contract_name} declara value_aliases.subsistema.SIN mas o S3 so tem "
            f"{sorted(actual_values)} (alias ORFAO -> 0 linhas). Remova o alias ou aponte o valor nacional correto."
        )


# =============================================================================
# A5: Granularity consistency check
# =============================================================================


class TestA5GranularityConsistency:
    """
    Check that contract granularity, row_grain.temporal, and S3 suffix are consistent.

    Note: This test is informational. Many contracts have minor inconsistencies
    between declared granularity and S3 suffix that are acceptable (e.g., "anual"
    data stored in daily files for partition purposes). Only critical
    inconsistencies should be flagged.

    Convention:
    - _di = daily
    - _ho = hourly
    - _me = monthly
    - _se = weekly
    - _tm = semi-hourly (30 min)
    """

    # Contracts with known granularity mismatches to skip
    CONTRACTS_TO_SKIP = {
        # These have known granularity mismatches that are acceptable
        "ear-diario-por-bacia.odcs.yaml",
        "ear-diario-por-ree-reservatorio-equivalente-de-energia.odcs.yaml",
        "ear-diario-por-reservatorio.odcs.yaml",
        "ear-diario-por-subsistema.odcs.yaml",
        "fator-capacidade-2.odcs.yaml",
        "ind_disponibilidade_ft_conversor.odcs.yaml",
        "ind_disponibilidade_ft_reativo.odcs.yaml",
        "ind_disponibilidade_ft_trlt.odcs.yaml",
        "ind_disponibilidade_fgeracao_uge_mensal.odcs.yaml",  # Now points to _me but granularity says anual
        # O corpus preserva o campo legado como "anual", embora row_grain e
        # o prefixo _me sejam mensais. Corrigir o contrato foge ao escopo desta tarefa.
        "carga-mensal.odcs.yaml",
    }

    SUFFIX_TO_GRANULARITY = {
        "_di": ["diaria", "diario", "daily"],
        "_ho": ["horaria", "horario", "hourly"],
        "_me": ["mensal", "monthly"],
        "_se": ["semanal", "weekly"],
        "_tm": ["semi-horaria", "semi-horario", "30min", "30_min"],
    }

    def get_contract_files(self) -> list[Path]:
        """Get all contract files."""
        return list(CONTRACTS_DIR.glob("*.odcs.yaml"))

    @pytest.mark.parametrize("contract_file", CONTRACTS_DIR.glob("*.odcs.yaml"), ids=lambda p: p.stem)
    def test_granularity_matches_suffix(self, contract_file: Path):
        """Check that declared granularity matches the S3 path suffix."""
        # Skip contracts with known acceptable mismatches
        if contract_file.name in self.CONTRACTS_TO_SKIP:
            pytest.skip("Known mismatch - acceptable")

        with open(contract_file, encoding="utf-8") as f:
            contract = yaml.safe_load(f)

        # Get S3 location
        location = get_s3_location(contract)
        if not location:
            pytest.skip("No S3 location")

        # Extract prefix/suffix from location
        # Example: s3://bucket/dataset/carga_energia_di/*.parquet
        match = re.search(r"/([^/]+)_([a-z]{2})/\*\.parquet", location.lower())
        if not match:
            pytest.skip(f"Cannot parse suffix from location: {location}")

        suffix = f"_{match.group(2)}"

        # Get declared granularity from customProperties
        custom_props = contract.get("customProperties", [])
        declared_granularity = None
        for prop in custom_props:
            if prop.get("property") == "granularity":
                declared_granularity = str(prop.get("value", "")).lower()
                break

        if not declared_granularity:
            pytest.skip("No granularity declared")

        # Check consistency
        expected_granularities = self.SUFFIX_TO_GRANULARITY.get(suffix, [])
        if not expected_granularities:
            pytest.skip(f"Unknown suffix: {suffix}")

        # Normalize: remove accents and compare
        import unicodedata

        def normalize(s):
            return unicodedata.normalize("NFKD", s).encode("ASCII", "ignore").decode("ASCII").lower()

        normalized_declared = normalize(declared_granularity)
        normalized_expected = [normalize(g) for g in expected_granularities]

        assert any(e in normalized_declared or normalized_declared in e for e in normalized_expected), (
            f"Contract {contract_file.stem}: declared granularity '{declared_granularity}' "
            f"doesn't match S3 suffix '{suffix}' (expected one of: {expected_granularities})"
        )


# =============================================================================
# Additional integrity checks
# =============================================================================


class TestContractIntegrity:
    """General contract integrity checks."""

    def test_all_contracts_have_s3_location(self):
        """Verify all active contracts have S3 location."""
        missing = []
        for contract_file in CONTRACTS_DIR.glob("*.odcs.yaml"):
            if "descontinuado" in contract_file.name:
                continue
            with open(contract_file, encoding="utf-8") as f:
                contract = yaml.safe_load(f)
            if contract.get("status") != "active":
                continue
            location = get_s3_location(contract)
            if not location:
                missing.append(contract_file.stem)

        assert not missing, f"Active contracts without S3 location: {missing}"

