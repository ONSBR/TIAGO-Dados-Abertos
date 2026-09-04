# -*- coding: utf-8 -*-
"""Tests for SQL validator hardening against adversarial payloads.

TDD: These tests must FAIL on the old regex-based validator and PASS after
migrating to AST-based validation with allowlists.
"""

import multiprocessing

import pytest

# Ensure src is importable
from mcp_tiago_dados_abertos.execucao.validator import SqlValidator


# Create validator instance (con=None because we use sqlglot for validation now)
@pytest.fixture
def validator():
    return SqlValidator(con=None)


# ==============================================================================
# CRITICAL BYPASSES - MUST BE REJECTED
# ==============================================================================


class TestCriticalExfilBypass:
    """Credential exfiltration via comment/quote injection."""

    @pytest.mark.parametrize(
        "sql,desc",
        [
            # Block comment between function name and (
            ("SELECT current_setting/**/('s3_secret_access_key')", "block comment current_setting"),
            ("SELECT current_setting /*x*/ ('s3_secret_access_key')", "block comment with space"),
            ("SELECT value FROM duckdb_settings/**/() WHERE name='s3_secret_access_key'", "duckdb_settings block"),
            ("SELECT getenv/**/('AWS_SECRET_ACCESS_KEY')", "getenv block comment"),
            ("SELECT * FROM duckdb_secrets/**/()", "duckdb_secrets block comment"),
            (
                "WITH s AS (SELECT value v FROM duckdb_settings/**/() WHERE name='s3_secret_access_key') SELECT * FROM s",  # noqa: E501
                "CTE with duckdb_settings block",
            ),
            # Line comment between name and (
            ("SELECT current_setting --c\n('s3_secret_access_key')", "line comment current_setting"),
            ("SELECT value FROM duckdb_settings --\n() WHERE name='s3_access_key_id'", "line comment duckdb_settings"),
            # Double-quoted identifier
            ("SELECT \"current_setting\"('s3_secret_access_key')", "quoted identifier current_setting"),
            ("SELECT value FROM \"duckdb_settings\"() WHERE name='s3_secret_access_key'", "quoted duckdb_settings"),
            ("SELECT \"getenv\"('AWS_SECRET_ACCESS_KEY')", "quoted getenv"),
            # json_execute_serialized_sql (not in any blocklist)
            (
                'SELECT * FROM json_execute_serialized_sql(\'{"type":"select"}\')',
                "json_execute_serialized_sql",
            ),
            # Other introspection functions
            ("SELECT getvariable/**/('x')", "getvariable block comment"),
            ("SELECT which_secret('s3://x','s3')", "which_secret"),
            ("SELECT * FROM duckdb_secret_types()", "duckdb_secret_types"),
            ("SELECT write_log('pentest')", "write_log"),
            # Combined attacks
            ("SELECT \"getvariable\"('x')", "quoted getvariable"),
            ("SELECT pragma_database_list/**/()", "pragma block comment"),
        ],
    )
    def test_exfil_blocked(self, validator, sql, desc):
        ok, msg = validator.is_safe(sql)
        assert not ok, f"[{desc}] should be blocked: {sql}"


class TestPgSettingsExfil:
    """pg_settings view (no function call, no trick needed)."""

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT setting FROM pg_settings WHERE name='s3_secret_access_key'",
            "SELECT * FROM pg_catalog.pg_settings",
            "SELECT setting FROM pg_settings",
            "SELECT * FROM information_schema.tables",
            "SELECT * FROM duckdb_tables()",
            "SELECT * FROM duckdb_columns()",
            "SELECT * FROM sqlite_master",
        ],
    )
    def test_system_tables_blocked(self, validator, sql):
        ok, _ = validator.is_safe(sql)
        assert not ok, f"should block system table/view access: {sql}"


class TestArbitraryS3Bucket:
    """Reading arbitrary S3 buckets (no bucket allowlist)."""

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT * FROM read_parquet('s3://some-victim-bucket/secret.parquet')",
            "SELECT * FROM read_parquet('S3://SOME-VICTIM-BUCKET/secret.parquet')",
            "SELECT * FROM read_parquet('s3://attacker-controlled/data.parquet')",
            "SELECT * FROM read_csv('s3://not-ons-bucket/file.csv')",
            # Second element in list bypass
            "SELECT * FROM read_parquet(['s3://ons-aws-prod-opendata/dataset/x.parquet', 's3://evil/y.parquet'])",
        ],
    )
    def test_non_ons_bucket_blocked(self, validator, sql):
        ok, _ = validator.is_safe(sql)
        assert not ok, f"should block non-ONS bucket: {sql}"


class TestReaderBypass:
    """Readers outside allowlist via comment/quote injection."""

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT * FROM read_json_auto/**/('s3://x/a.json')",
            "SELECT * FROM read_text/**/('s3://x/a.txt')",
            "SELECT * FROM read_blob/**/('s3://x/a.bin')",
            "SELECT * FROM \"read_json\"('s3://attacker-bucket/x.json')",
            "SELECT * FROM \"read_text\"('s3://x/a.txt')",
            "SELECT * FROM read_ndjson/**/('s3://x/a.ndjson')",
            "SELECT * FROM json_scan/**/('s3://x/a.json')",
            "SELECT * FROM parquet_scan/**/('s3://x/a.parquet')",
        ],
    )
    def test_reader_bypass_blocked(self, validator, sql):
        ok, _ = validator.is_safe(sql)
        assert not ok, f"should block reader bypass: {sql}"


class TestCrashDoS:
    """Crash/DoS via giant literals causing constant-folding OOM."""

    def test_repeat_giant_literal_rejected_not_crash(self, validator):
        """Must reject without crashing the process."""
        sql = "SELECT repeat('x', 2000000000)"
        ok, msg = validator.is_safe(sql)
        assert not ok, "should reject giant literal in repeat()"
        assert "literal" in msg.lower() or "expressao" in msg.lower() or "potencialmente" in msg.lower()

    def test_range_giant_literal_rejected(self, validator):
        sql = "SELECT count(*) FROM (SELECT unnest(range(1, 2000000000)))"
        ok, msg = validator.is_safe(sql)
        assert not ok, "should reject giant literal in range()"

    def test_generate_series_giant_rejected(self, validator):
        sql = "SELECT * FROM generate_series(1, 1000000000)"
        ok, _ = validator.is_safe(sql)
        assert not ok, "should reject giant literal in generate_series()"

    def test_nested_repeat_rejected(self, validator):
        sql = "SELECT length(repeat('x', 1000000000)) FROM range(1000000)"
        ok, _ = validator.is_safe(sql)
        assert not ok, "should reject giant nested literal"


class TestCostCaps:
    """DoS via expensive operations."""

    def test_with_recursive_rejected(self, validator):
        sql = """
        WITH RECURSIVE bomb AS (
            SELECT 1 AS n
            UNION ALL
            SELECT n + 1 FROM bomb WHERE n < 1000000000
        )
        SELECT * FROM bomb
        """
        ok, _ = validator.is_safe(sql)
        assert not ok, "should reject WITH RECURSIVE"

    def test_cross_join_cartesian_rejected(self, validator):
        """Cross join without condition creating cartesian product."""
        sql = """
        SELECT * FROM read_parquet('s3://ons-aws-prod-opendata/dataset/a/*.parquet') a,
                      read_parquet('s3://ons-aws-prod-opendata/dataset/b/*.parquet') b,
                      read_parquet('s3://ons-aws-prod-opendata/dataset/c/*.parquet') c
        """
        ok, _ = validator.is_safe(sql)
        assert not ok, "should reject unconstrained cartesian product"


# ==============================================================================
# BLOCKED - functions that must NEVER pass (comprehensive blocklist)
# ==============================================================================


class TestBlockedFunctions:
    """Functions that must be blocked regardless of form."""

    @pytest.mark.parametrize(
        "sql",
        [
            # Introspection
            "SELECT current_setting('memory_limit')",
            "SELECT * FROM duckdb_settings()",
            "SELECT * FROM duckdb_secrets()",
            "SELECT * FROM duckdb_variables()",
            "SELECT getvariable('x')",
            "SELECT getenv('PATH')",
            "SELECT * FROM pragma_database_list()",
            "SELECT pragma_version()",
            # File/network readers not in allowlist
            "SELECT * FROM read_ndjson('s3://ons-aws-prod-opendata/x.ndjson')",
            "SELECT * FROM read_text('s3://ons-aws-prod-opendata/x.txt')",
            "SELECT * FROM read_blob('s3://ons-aws-prod-opendata/x.bin')",
            "SELECT * FROM glob('s3://ons-aws-prod-opendata/*')",
            "SELECT * FROM parquet_metadata('s3://ons-aws-prod-opendata/x.parquet')",
            "SELECT * FROM parquet_schema('s3://ons-aws-prod-opendata/x.parquet')",
            "SELECT * FROM sniff_csv('s3://ons-aws-prod-opendata/x.csv')",
            "SELECT * FROM json_scan('s3://ons-aws-prod-opendata/x.json')",
            "SELECT * FROM parquet_scan('s3://ons-aws-prod-opendata/x.parquet')",
            "SELECT * FROM csv_scan('s3://ons-aws-prod-opendata/x.csv')",
            # Serialization
            "SELECT json_serialize_sql('SELECT 1')",
            "SELECT * FROM json_execute_serialized_sql('{}')",
            # System functions
            "SELECT copy_database('a', 'b')",
            "SELECT write_log('test')",
            "SELECT which_secret('s3://x', 's3')",
            "SELECT * FROM duckdb_secret_types()",
        ],
    )
    def test_blocked_function(self, validator, sql):
        ok, _ = validator.is_safe(sql)
        assert not ok, f"should block: {sql}"


# ==============================================================================
# NON-REGRESSION: Valid SQL that MUST continue to work
# ==============================================================================


class TestValidSqlRegression:
    """SQL patterns from contracts that must continue to pass validation."""

    @pytest.mark.parametrize(
        "sql",
        [
            # Basic SELECT with allowed reader
            "SELECT * FROM read_parquet('s3://ons-aws-prod-opendata/dataset/carga_energia_di/*.parquet') LIMIT 1",
            "SELECT * FROM read_csv('s3://ons-aws-prod-opendata/dataset/x.csv')",
            "SELECT * FROM read_csv_auto('s3://ons-aws-prod-opendata/dataset/x.csv')",
            "SELECT * FROM read_json('s3://ons-aws-prod-opendata/dataset/x.json')",
            "SELECT * FROM read_json_auto('s3://ons-aws-prod-opendata/dataset/x.json')",
            "SELECT * FROM read_json('https://dados.ons.org.br/dataset/x.json')",
            "SELECT * FROM read_json_auto('https://www.ons.org.br/dataset/x.json')",
            # List form
            "SELECT * FROM read_parquet(['s3://ons-aws-prod-opendata/dataset/a.parquet', 's3://ons-aws-prod-opendata/dataset/b.parquet'])",
            # CTE
            "WITH t AS (SELECT 1 AS a) SELECT a FROM t",
            # Real contract patterns
            """
            SELECT id_subsistema, nom_subsistema,
                   ROUND(SUM(TRY_CAST(val_gerhidraulica AS DOUBLE)), 2) AS geracao_total
            FROM read_parquet('s3://ons-aws-prod-opendata/dataset/balanco_energia_subsistema_ho/*.parquet', union_by_name=true)
            WHERE YEAR(TRY_CAST(din_instante AS TIMESTAMP)) = 2024
            GROUP BY id_subsistema, nom_subsistema
            ORDER BY geracao_total DESC
            """,  # noqa: E501
            # String with keywords inside (not a command)
            "SELECT replace(nom_subsistema, 'X', 'Y') FROM read_parquet('s3://ons-aws-prod-opendata/dataset/x.parquet')",
            "SELECT payload FROM read_parquet('s3://ons-aws-prod-opendata/dataset/x.parquet')",
            "SELECT * FROM read_parquet('s3://ons-aws-prod-opendata/dataset/x.parquet') WHERE tipo = 'COPY'",
            # Window functions
            """
            SELECT id_subsistema, din_instante, val_carga,
                   ROW_NUMBER() OVER (PARTITION BY id_subsistema ORDER BY val_carga DESC) AS rn
            FROM read_parquet('s3://ons-aws-prod-opendata/dataset/balanco_energia_subsistema_ho/*.parquet')
            WHERE din_instante >= '2024-01-01'
            """,
            # Aggregations
            """
            SELECT DATE_TRUNC('month', din_instante) AS mes,
                   AVG(val_gereolica) AS media_eolica,
                   SUM(val_carga) AS total_carga,
                   COUNT(*) AS registros,
                   MIN(val_carga) AS min_carga,
                   MAX(val_carga) AS max_carga
            FROM read_parquet('s3://ons-aws-prod-opendata/dataset/balanco_energia_subsistema_ho/*.parquet')
            GROUP BY DATE_TRUNC('month', din_instante)
            """,
            # CASE expressions
            """
            SELECT CASE WHEN val_carga > 1000 THEN 'alto' ELSE 'baixo' END AS nivel
            FROM read_parquet('s3://ons-aws-prod-opendata/dataset/carga_energia_di/*.parquet')
            """,
            # Subqueries
            """
            SELECT * FROM (
                SELECT id_subsistema, MAX(val_carga) AS max_carga
                FROM read_parquet('s3://ons-aws-prod-opendata/dataset/carga_energia_di/*.parquet')
                GROUP BY id_subsistema
            ) AS sub
            WHERE max_carga > 1000
            """,
            # COALESCE and NULLIF
            """
            SELECT COALESCE(val_carga, 0) AS carga,
                   NULLIF(val_geracao, 0) AS geracao
            FROM read_parquet('s3://ons-aws-prod-opendata/dataset/x/*.parquet')
            """,
            # Date functions
            """
            SELECT YEAR(din_instante) AS ano,
                   MONTH(din_instante) AS mes,
                   DATE_TRUNC('day', din_instante) AS dia,
                   CURRENT_DATE AS hoje
            FROM read_parquet('s3://ons-aws-prod-opendata/dataset/x/*.parquet')
            """,
            # String functions
            """
            SELECT TRIM(nom_estado) AS estado,
                   UPPER(nom_subsistema) AS subsistema,
                   LOWER(nom_bacia) AS bacia,
                   LENGTH(nom_usina) AS len,
                   SUBSTRING(cod_equipamento, 1, 3) AS prefixo
            FROM read_parquet('s3://ons-aws-prod-opendata/dataset/x/*.parquet')
            """,
            # Math functions
            """
            SELECT ROUND(val_geracao, 2) AS geracao,
                   ABS(val_desvio) AS desvio_abs,
                   CEIL(val_percentual) AS pct_ceil,
                   FLOOR(val_percentual) AS pct_floor,
                   SQRT(val_potencia) AS raiz
            FROM read_parquet('s3://ons-aws-prod-opendata/dataset/x/*.parquet')
            """,
            # ILIKE pattern matching
            """
            SELECT * FROM read_parquet('s3://ons-aws-prod-opendata/dataset/x/*.parquet')
            WHERE nom_bacia ILIKE '%SAO FRANCISCO%'
            """,
            # BETWEEN and IN
            """
            SELECT * FROM read_parquet('s3://ons-aws-prod-opendata/dataset/x/*.parquet')
            WHERE val_carga BETWEEN 100 AND 500
              AND id_subsistema IN ('NE', 'SE', 'S')
            """,
            # DISTINCT
            """
            SELECT DISTINCT id_subsistema, nom_subsistema
            FROM read_parquet('s3://ons-aws-prod-opendata/dataset/x/*.parquet')
            """,
            # UNION
            """
            SELECT id_subsistema, val_carga FROM read_parquet('s3://ons-aws-prod-opendata/dataset/a/*.parquet')
            UNION ALL
            SELECT id_subsistema, val_carga FROM read_parquet('s3://ons-aws-prod-opendata/dataset/b/*.parquet')
            """,
            # INNER JOIN
            """
            SELECT a.id_subsistema, a.val_carga, b.val_geracao
            FROM read_parquet('s3://ons-aws-prod-opendata/dataset/a/*.parquet') AS a
            INNER JOIN read_parquet('s3://ons-aws-prod-opendata/dataset/b/*.parquet') AS b
            ON a.id_subsistema = b.id_subsistema AND a.din_instante = b.din_instante
            """,
            # LEFT JOIN
            """
            SELECT a.*, b.val_extra
            FROM read_parquet('s3://ons-aws-prod-opendata/dataset/a/*.parquet') AS a
            LEFT JOIN read_parquet('s3://ons-aws-prod-opendata/dataset/b/*.parquet') AS b
            ON a.id = b.id
            """,
        ],
    )
    def test_valid_sql_passes(self, validator, sql):
        ok, msg = validator.is_safe(sql)
        assert ok, f"should allow valid SQL: {sql}\nRejection reason: {msg}"


class TestDDLDMLStillBlocked:
    """DDL/DML must still be blocked (existing behavior)."""

    @pytest.mark.parametrize(
        "sql",
        [
            "DROP TABLE users",
            "DELETE FROM users",
            "INSERT INTO users VALUES (1)",
            "UPDATE users SET name = 'x'",
            "CREATE TABLE t (a INT)",
            "ALTER TABLE t ADD COLUMN b INT",
            "TRUNCATE TABLE t",
            "COPY t TO 'file.csv'",
            "ATTACH DATABASE 'x.db'",
            "DETACH DATABASE x",
            "EXPORT DATABASE '/tmp'",
            "IMPORT DATABASE '/tmp'",
            "INSTALL httpfs",
            "PRAGMA threads=4",
            "CALL system('ls')",
            "SET memory_limit='8GB'",
            "VACUUM",
            "CHECKPOINT",
            "MERGE INTO t USING s ON t.id = s.id",
            "GRANT SELECT ON t TO user",
            "REVOKE SELECT ON t FROM user",
        ],
    )
    def test_ddl_dml_blocked(self, validator, sql):
        ok, _ = validator.is_safe(sql)
        assert not ok, f"should block DDL/DML: {sql}"


class TestStatementStacking:
    """Multiple statements must still be blocked."""

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT 1; LOAD httpfs",
            "SELECT 1; RESET memory_limit",
            "SELECT 1; USE memory",
            "SELECT 1; DROP TABLE x",
            "SELECT 1;SELECT 2",
        ],
    )
    def test_stacking_blocked(self, validator, sql):
        ok, _ = validator.is_safe(sql)
        assert not ok, f"should block statement stacking: {sql}"


class TestLFISSRF:
    """Local file and SSRF attacks must still be blocked."""

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT * FROM read_parquet('/etc/passwd')",
            "SELECT * FROM read_csv('/tmp/secret.csv')",
            "SELECT * FROM read_parquet('C:\\Windows\\System32\\config\\SAM')",
            "SELECT * FROM read_parquet('\\\\attacker\\share\\file.parquet')",
            "SELECT * FROM read_parquet('file:///etc/passwd')",
            "SELECT * FROM read_csv('http://169.254.169.254/latest/meta-data/')",
            "SELECT * FROM read_parquet('https://evil.com/x.parquet')",
            "SELECT * FROM read_json('https://evil.com/x.json')",
            "SELECT * FROM read_json('https://ons.org.br.evil.com/x.json')",
            "SELECT * FROM read_json('http://dados.ons.org.br/x.json')",
            "SELECT * FROM read_csv('./local.csv')",
            "SELECT * FROM read_csv('../../../etc/passwd')",
        ],
    )
    def test_lfi_ssrf_blocked(self, validator, sql):
        ok, _ = validator.is_safe(sql)
        assert not ok, f"should block LFI/SSRF: {sql}"


# ==============================================================================
# SYNTAX validation
# ==============================================================================


class TestSyntaxValidation:
    """SQL syntax must be validated."""

    def test_must_start_with_select_or_with(self, validator):
        ok, _ = validator.is_valid_syntax("EXPLAIN SELECT 1")
        assert not ok

    def test_describe_allowed(self, validator):
        # DESCRIBE is allowed per existing behavior
        ok, _ = validator.is_valid_syntax("DESCRIBE SELECT 1")
        assert ok

    def test_invalid_syntax_rejected(self, validator):
        ok, _ = validator.validate("SELECT * FORM table")  # typo: FORM instead of FROM
        assert not ok


# ==============================================================================
# CRASH SAFETY - must not crash pytest process
# ==============================================================================


def _run_crash_test():
    """Run in subprocess to detect crashes."""
    from mcp_tiago_dados_abertos.execucao.validator import SqlValidator

    v = SqlValidator(con=None)
    # These would crash old validator via EXPLAIN constant-folding
    payloads = [
        "SELECT repeat('x', 2000000000)",
        "SELECT count(*) FROM (SELECT unnest(range(1, 2000000000)))",
        "SELECT length(repeat('x', 1000000000)) FROM range(1000000)",
    ]
    for sql in payloads:
        try:
            v.validate(sql)
        except Exception:
            pass  # Exception is fine, crash is not
    return True


class TestNoCrash:
    """Validator must not crash the process."""

    def test_no_crash_on_giant_literals(self):
        """Run crash-prone payloads in subprocess to verify no crash."""
        ctx = multiprocessing.get_context("spawn")
        with ctx.Pool(1) as pool:
            result = pool.apply(_run_crash_test)
            assert result is True, "Validator crashed on giant literal"
