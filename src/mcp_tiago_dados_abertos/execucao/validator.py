# -*- coding: utf-8 -*-
"""Validador SQL baseado em AST (sqlglot) para exposicao anonima.

Migrado de regex para AST-based validation com allowlists. Resolve bypasses:
  - Comentario/quote entre funcao e parentese: AST normaliza
  - pg_settings/system tables: allowlist de relacoes
  - json_execute_serialized_sql: allowlist de funcoes
  - Bucket S3 arbitrario: allowlist de buckets
  - Crash via EXPLAIN de literal gigante: parse-only + reject literal
  - DoS via custo: reject RECURSIVE, cap de cartesian joins

Defense-in-depth: mesmo que um bypass escape o AST, o backstop de regex permanece.
"""

import logging
import re
from typing import Optional

import sqlglot
from sqlglot import exp

logger = logging.getLogger("mcp_tiago_dados_abertos.validator")

# ==============================================================================
# CONFIGURATION: Allowlists and limits
# ==============================================================================

# Buckets S3 permitidos (prefixos). O ONS usa SO ons-aws-prod-opendata.
# A barra final e obrigatoria: impede domain-squat (s3://ons-aws-prod-opendata.evil.com/)
# e buckets nao autorizados (s3://example-blocked-bucket/).
ALLOWED_S3_BUCKET_PREFIXES = ("s3://ons-aws-prod-opendata/",)

# Hosts HTTPS permitidos. So o dominio OFICIAL do ONS; qualquer outro https://
# (ex.: evil.com) e bloqueado — fecha SSRF. Match por SUFIXO de dominio precedido
# de '.' ou host exato (impede squat tipo ons.org.br.evil.com).
ALLOWED_HTTPS_HOST_SUFFIXES = ("ons.org.br",)

# Leitores permitidos (lowercase). Apenas estes podem acessar dados.
# read_json/read_json_auto: fontes que devolvem JSON em vez de CSV/Parquet.
ALLOWED_READERS = frozenset(
    {"read_parquet", "read_csv", "read_csv_auto", "read_json", "read_json_auto"}
)

# Funcoes BLOQUEADAS incondicionalmente (introspecao, sistema, IO perigoso).
# Normalizadas para lowercase sem aspas.
BLOCKED_FUNCTIONS = frozenset(
    {
        # Introspection / credential exfil
        "current_setting",
        "duckdb_settings",
        "duckdb_secrets",
        "duckdb_variables",
        "getvariable",
        "getenv",
        "which_secret",
        "duckdb_secret_types",
        # Pragma introspection
        "pragma_database_list",
        "pragma_version",
        "pragma_table_info",
        "pragma_storage_info",
        # System catalog functions
        "duckdb_tables",
        "duckdb_columns",
        "duckdb_functions",
        "duckdb_views",
        "duckdb_schemas",
        "duckdb_databases",
        "duckdb_types",
        "duckdb_constraints",
        "duckdb_indexes",
        "duckdb_extensions",
        "duckdb_dependencies",
        "duckdb_keywords",
        "duckdb_optimizers",
        "duckdb_memory",
        # File/network readers NOT in allowlist
        # read_json / read_json_auto: LIBERADOS (ha' fonte que devolve JSON) — ja no
        # ALLOWED_READERS + o path passa pela allowlist de host HTTPS. read_json_objects/
        # read_ndjson seguem bloqueados (nao usados pelas fontes).
        "read_json_objects",
        "read_ndjson",
        "read_ndjson_auto",
        "read_ndjson_objects",
        "read_text",
        "read_blob",
        "glob",
        "parquet_metadata",
        "parquet_schema",
        "sniff_csv",
        # *_scan family
        "json_scan",
        "parquet_scan",
        "csv_scan",
        "arrow_scan",
        "delta_scan",
        "iceberg_scan",
        # Serialization (can embed malicious SQL)
        "json_serialize_sql",
        "json_execute_serialized_sql",
        # Database operations
        "copy_database",
        "attach_database",
        "detach_database",
        # Logging
        "write_log",
    }
)

# Tabelas/views de sistema BLOQUEADAS (lowercase)
BLOCKED_TABLES = frozenset(
    {
        "pg_settings",
        "pg_catalog",
        "information_schema",
        "sqlite_master",
        "sqlite_temp_master",
    }
)

# Schemas de sistema BLOQUEADOS (prefixos lowercase)
BLOCKED_SCHEMA_PREFIXES = ("duckdb_", "pg_", "information_schema", "sqlite_")

# DML/DDL e comandos perigosos (mantido como backstop, lowercase)
_BLOCKED_KEYWORDS = [
    "drop",
    "delete",
    "insert",
    "update",
    "create",
    "alter",
    "truncate",
    "copy",
    "attach",
    "detach",
    "export",
    "import",
    "install",
    "pragma",
    "call",
    "set",
    "vacuum",
    "checkpoint",
    "merge",
    "grant",
    "revoke",
    "load",
    "reset",
    "use",
]
_BLOCKED_RE = re.compile(r"\b(" + "|".join(_BLOCKED_KEYWORDS) + r")\b", re.IGNORECASE)

# Literais de string (para backstop)
_STRING_LITERAL_RE = re.compile(r"'(?:[^']|'')*'")
# Comentario de linha (-- ate o fim da linha) e de bloco (/* ... */), sem aninhamento.
_COMMENT_RE = re.compile(r"--[^\n]*|/\*.*?\*/", re.S)

# HTTP/LFI (backstop). http:// cleartext = SEMPRE bloqueado (IMDS/metadata/SSRF).
# https:// = permitido SO para hosts na allowlist (ALLOWED_HTTPS_HOST_SUFFIXES).
_HTTP_CLEARTEXT_RE = re.compile(r"['\"]\s*http://", re.IGNORECASE)
_HTTPS_HOST_RE = re.compile(r"['\"]\s*https://([^/'\"\s]+)", re.IGNORECASE)


# Caracteres que NUNCA aparecem na autoridade de uma URL legitima das fontes, e que
# mudam onde o host termina para quem interpreta a URL: `@` (userinfo), `#`
# (fragmento), `?` (query), barra invertida, `%` (escape) e espaco. A versao
# anterior cortava no ULTIMO `@`: `https://evil#@dados.ons.org.br/` passava como
# dados.ons.org.br e o DuckDB conectava em evil.
_AUTORIDADE_PROIBIDA = frozenset("@#?\\% \t\r\n")


def _https_host_allowed(authority: str) -> bool:
    """Autoridade https (host[:porta]) na allowlist? Falha FECHADO em qualquer
    caractere que redefina onde o host termina. Match exato ou subdominio
    (.dominio) — impede squat."""
    if not authority or any(c in _AUTORIDADE_PROIBIDA for c in authority):
        return False
    if authority.count(":") > 1:  # IPv6 ou porta dupla: fora
        return False
    h = authority.lower().split(":")[0]
    return bool(h) and any(h == d or h.endswith("." + d) for d in ALLOWED_HTTPS_HOST_SUFFIXES)
_LOCAL_PATH_LITERAL_RE = re.compile(r"""['"]\s*(/|\./|\.\./|[a-zA-Z]:[\\/]|\\\\|file://)""", re.IGNORECASE)

# Limite de valor literal em funcoes geradoras (anti-crash DoS)
MAX_LITERAL_VALUE = 10_000_000

# Funcoes que aceitam literais gigantes e podem causar crash/DoS
DANGEROUS_GENERATOR_FUNCTIONS = frozenset(
    {
        "repeat",
        "range",
        "generate_series",
        "unnest",
        "list_value",
    }
)

# Maximo de fontes em cross join sem condicao (anti-cartesian DoS)
MAX_UNCONSTRAINED_SOURCES = 2


# ==============================================================================
# AST-BASED VALIDATION
# ==============================================================================


def _normalize_name(name: str) -> str:
    """Remove aspas e normaliza para lowercase."""
    if name.startswith('"') and name.endswith('"'):
        name = name[1:-1]
    return name.lower()


def _extract_function_name(node) -> Optional[str]:
    """Extrai o nome normalizado de uma funcao do AST."""
    if isinstance(node, exp.Anonymous):
        return _normalize_name(node.name)
    if isinstance(node, exp.Func):
        # Para funcoes builtin, sqlglot usa classes especificas (ex: GenerateSeries, Repeat)
        # O nome da classe em snake_case corresponde ao nome SQL
        class_name = node.__class__.__name__
        # Converte CamelCase para snake_case
        snake_name = ""
        for i, c in enumerate(class_name):
            if c.isupper() and i > 0:
                snake_name += "_"
            snake_name += c.lower()

        # Tenta sql_name() primeiro (para Anonymous e algumas funcoes)
        if hasattr(node, "sql_name"):
            sql_name_result = node.sql_name()
            if sql_name_result:
                return _normalize_name(sql_name_result)

        # Usa o nome da classe convertido
        if snake_name and snake_name not in ("func", "anonymous"):
            return snake_name

        # Fallback para o atributo name
        if hasattr(node, "name") and node.name:
            return _normalize_name(node.name)
    return None


def _check_blocked_functions(root: exp.Expression) -> Optional[str]:
    """Verifica se ha funcoes bloqueadas na AST."""
    # Itera sobre todos os nos do tipo funcao
    for node in root.walk():
        name = _extract_function_name(node)
        if name and name in BLOCKED_FUNCTIONS:
            return f"Funcao '{name}' bloqueada (introspecao/sistema proibida)."
        # Funcoes que comecam com pragma_
        if name and name.startswith("pragma_"):
            return f"Funcao '{name}' bloqueada (introspecao via pragma)."
        # Qualquer funcao interna/introspecao do DuckDB (duckdb_current_setting,
        # duckdb_execute, duckdb_prepare, duckdb_files, duckdb_licenses, ...)
        if name and name.startswith("duckdb_"):
            return f"Funcao '{name}' bloqueada (introspecao/interna do DuckDB)."
    return None


def _check_blocked_tables(root: exp.Expression) -> Optional[str]:
    """Verifica se ha tabelas/views de sistema bloqueadas."""
    for table in root.find_all(exp.Table):
        table_name = _normalize_name(table.name) if table.name else ""
        db_name = _normalize_name(table.db) if table.db else ""
        catalog = _normalize_name(table.catalog) if table.catalog else ""

        # Verifica nome da tabela
        if table_name in BLOCKED_TABLES:
            return f"Acesso a tabela de sistema '{table_name}' bloqueado."

        # Verifica schema/database prefix
        full_path = f"{catalog}.{db_name}.{table_name}".lower()
        for prefix in BLOCKED_SCHEMA_PREFIXES:
            if prefix in full_path:
                return f"Acesso a schema de sistema '{prefix}*' bloqueado."

        # Verifica se e uma funcao table-valued (ex: duckdb_tables())
        # que pode aparecer como Table no AST
        if any(table_name.startswith(p) for p in BLOCKED_SCHEMA_PREFIXES):
            return f"Acesso a funcao de sistema '{table_name}' bloqueado."

    return None


def _literal_paths(arg) -> Optional[list[str]]:
    """Caminho(s) do argumento de um leitor, ou None se a forma nao e' aceita.

    Aceita SO `exp.Literal` de string, ou `exp.Array` cujos elementos sao todos
    literais de string. Tudo mais — RawString ($$..$$), ByteString (E'..'),
    DPipe (chr()||...), Subquery, Column — e' recusado. Antes, essas formas
    rendiam lista VAZIA e o vazio era aprovado: leitura de bucket arbitrario e
    SSRF chegaram ao DuckDB por quatro vetores cada.
    """
    if isinstance(arg, exp.Literal) and arg.is_string:
        return [arg.this]
    if isinstance(arg, exp.Array):
        out = []
        for elem in arg.expressions:
            if not (isinstance(elem, exp.Literal) and elem.is_string):
                return None
            out.append(elem.this)
        return out
    return None


def _extract_s3_paths(root: exp.Expression) -> tuple[list[str], Optional[str]]:
    """(caminhos, erro). Erro quando algum leitor tem argumento de forma nao-literal."""
    paths: list[str] = []
    for node in root.walk():
        name = _extract_function_name(node)
        if not (name and name in ALLOWED_READERS):
            continue
        if hasattr(node, "expressions") and node.expressions:
            arg = node.expressions[0]
        elif getattr(node, "this", None) is not None:
            arg = node.this
        else:
            return paths, f"Leitor '{name}' sem caminho."
        found = _literal_paths(arg)
        if found is None:
            return paths, (
                f"Caminho de '{name}' deve ser literal de string simples (ou lista de literais); "
                f"forma '{type(arg).__name__}' nao permitida."
            )
        paths.extend(found)
    return paths, None


def _extract_string_literals(node) -> list[str]:
    """Extrai literais string de um no (incluindo listas)."""
    literals = []
    if isinstance(node, exp.Literal) and node.is_string:
        literals.append(node.this)
    elif isinstance(node, exp.Array):
        for elem in node.expressions:
            literals.extend(_extract_string_literals(elem))
    # Percorre filhos para encontrar mais literais
    if hasattr(node, "walk"):
        for child in node.walk():
            if child is not node and isinstance(child, exp.Literal) and child.is_string:
                literals.append(child.this)
    return literals


def _check_s3_bucket_allowlist(root: exp.Expression) -> Optional[str]:
    """Verifica se todo path de leitor esta autorizado: s3:// de bucket allowlisted
    OU https:// de host allowlisted. Qualquer outro e bloqueado."""
    paths, err = _extract_s3_paths(root)
    if err:
        return err
    for path in paths:
        path_lower = path.lower()
        if path_lower.startswith("s3://"):
            allowed = any(path_lower.startswith(p.lower()) for p in ALLOWED_S3_BUCKET_PREFIXES)
            if not allowed:
                bucket = path.split("/")[2] if len(path.split("/")) > 2 else path
                return f"Bucket S3 '{bucket}' nao autorizado. Use apenas dados do ONS."
        elif path_lower.startswith("https://"):
            host = path[len("https://"):].split("/")[0]
            if not _https_host_allowed(host):
                return f"Host HTTPS '{host}' nao autorizado. Fonte permitida: ONS."
        else:
            # nem s3:// nem https:// (local/http cleartext) — bloqueado
            return f"Caminho nao-autorizado bloqueado: '{path[:50]}...'"

    return None


_PATH_LIKE_TABLE_RE = re.compile(
    r"://|[/\\*]|\.\.|\.(parquet|csv|tsv|json|jsonl|ndjson|txt|gz|zst|xlsx?)$", re.IGNORECASE
)


def _check_implicit_path_tables(root: exp.Expression) -> Optional[str]:
    """`FROM 'caminho'` sem funcao de leitor: o DuckDB le o arquivo/URL como tabela.

    Nao passava por nenhuma checagem de caminho (e' Table, nao leitor): bucket S3
    arbitrario e LFI por caminho RELATIVO (`FROM 'x.parquet'`, fora do alcance da
    regex de caminho local) chegavam ao DuckDB. Aqui so' existe leitor explicito e
    CTE, entao nome de tabela que pareca caminho ou URL e' recusado.
    """
    for table in root.find_all(exp.Table):
        alvo = table.this
        if alvo is None or isinstance(alvo, exp.Func):
            continue  # read_parquet(...) e afins: tratados pela allowlist de caminho
        texto = alvo.name if isinstance(alvo, exp.Identifier) else str(getattr(alvo, "this", "") or "")
        if texto and _PATH_LIKE_TABLE_RE.search(texto):
            return (
                f"Tabela '{texto[:50]}' parece caminho/URL. Use read_parquet('s3://...') "
                "ou read_csv('https://...') explicitamente."
            )
    return None


def _check_non_allowed_readers(root: exp.Expression) -> Optional[str]:
    """Verifica se ha leitores fora da allowlist sendo usados."""
    for node in root.walk():
        name = _extract_function_name(node)
        if not name:
            continue

        # Detecta leitores pelo padrao de nome
        is_reader = (
            name.startswith("read_")
            or name.endswith("_scan")
            or name in {"glob", "parquet_metadata", "parquet_schema"}
            or name.startswith("sniff_")
        )

        if is_reader and name not in ALLOWED_READERS:
            return f"Leitor '{name}' nao permitido. Use read_parquet/read_csv sobre s3://."

    return None


def _const_eval(node) -> Optional[float]:
    """Avalia uma expressao aritmetica CONSTANTE (literais + */+-/parenteses/negacao).
    Retorna o valor float, ou None se nao for constante (ex.: envolve coluna).
    Fecha o bypass 'repeat(x, 1000000 * 1000000)' que escapa do cap de literal simples.
    """
    if node is None:
        return None
    if isinstance(node, exp.Paren):
        return _const_eval(node.this)
    if isinstance(node, exp.Neg):
        v = _const_eval(node.this)
        return -v if v is not None else None
    if isinstance(node, exp.Literal) and node.is_number:
        try:
            return float(node.this)
        except (ValueError, TypeError):
            return None
    if isinstance(node, (exp.Mul, exp.Add, exp.Sub, exp.Div)):
        left = _const_eval(node.left)
        right = _const_eval(node.right)
        if left is None or right is None:
            return None
        try:
            if isinstance(node, exp.Mul):
                return left * right
            if isinstance(node, exp.Add):
                return left + right
            if isinstance(node, exp.Sub):
                return left - right
            if isinstance(node, exp.Div):
                return left / right if right else None
        except (ValueError, TypeError, OverflowError, ZeroDivisionError):
            return None
    return None


def _check_giant_literals(root: exp.Expression) -> Optional[str]:
    """Verifica se ha literais numericos gigantes em funcoes perigosas ou em qualquer lugar."""

    # Primeiro, verifica literais em funcoes perigosas
    for node in root.walk():
        name = _extract_function_name(node)
        if name and name in DANGEROUS_GENERATOR_FUNCTIONS:
            # Coleta TODOS os argumentos da funcao, qualquer que seja o nome
            # (this/times/start/end/step/length/expressions...). Nao confiar em lista fixa:
            # exp.Repeat guarda o count em 'times', que uma lista fixa perde.
            args_to_check = []
            for v in node.args.values():
                if isinstance(v, exp.Expression):
                    args_to_check.append(v)
                elif isinstance(v, list):
                    args_to_check.extend(x for x in v if isinstance(x, exp.Expression))

            # Verifica cada argumento: literal OU aritmetica constante (ex.: 1e6 * 1e6)
            for arg in args_to_check:
                val = _const_eval(arg)
                if val is not None and abs(val) > MAX_LITERAL_VALUE:
                    return (
                        f"Expressao potencialmente cara: {name}() com valor "
                        f">{MAX_LITERAL_VALUE}. Reduza o valor ou use filtros."
                    )

    # Verifica literais muito grandes em qualquer lugar (anti constant-folding crash)
    for node in root.walk():
        if isinstance(node, exp.Literal) and node.is_number:
            try:
                val = float(node.this)
                # Limite alto para bloquear apenas valores realmente perigosos
                # (constant folding com literais gigantes). 1e9 e divisor corriqueiro em
                # analise financeira (R$ bi); teto vai a 1e12.
                if abs(val) >= 1_000_000_000_000:  # 1 trilhao ou mais
                    return (f"Literal numerico muito grande ({val:.0f}). O teto e 1e12: divida em etapas "
                            "(ex.: / 1000000 e depois / 1000000) ou filtre antes de escalar.")
            except (ValueError, TypeError):
                pass

    return None


def _check_recursive_cte(root: exp.Expression) -> Optional[str]:
    """Verifica se ha WITH RECURSIVE (DoS potencial)."""
    for node in root.walk():
        if isinstance(node, exp.With):
            # sqlglot marca CTEs recursivas com recursive=True
            if getattr(node, "recursive", False):
                return "WITH RECURSIVE nao permitido (potencial DoS). Use CTEs nao-recursivas."
    return None


def _count_from_sources(select_node: exp.Expression) -> int:
    """Conta o numero de fontes em FROM/JOIN de um SELECT especifico (nao recursivo em UNION).

    Para cross joins com virgula (FROM a, b, c), sqlglot coloca o primeiro no FROM
    e os demais como JOINs implicitos. Precisamos contar todos.
    """
    count = 0

    # Conta a fonte principal no FROM (se existir)
    # sqlglot usa "from_" como nome do argumento (from e palavra reservada Python)
    from_clause = select_node.args.get("from") or select_node.args.get("from_")
    if from_clause:
        count += 1

    # Conta JOINs diretos deste SELECT (incluindo cross joins com virgula)
    joins = select_node.args.get("joins") or []
    count += len(joins)

    return count


def _has_join_condition(select_node: exp.Expression) -> bool:
    """Verifica se ha condicao de join (ON, USING ou WHERE) no SELECT especifico."""
    # Tem clausula ON ou USING em algum JOIN. USING e condicao de igualdade tanto
    # quanto ON; sem esta linha, "a JOIN b USING (k) JOIN c USING (k)" era tratado
    # como produto cartesiano.
    for join in select_node.args.get("joins") or []:
        if join.args.get("on") or join.args.get("using"):
            return True
    # Tem WHERE
    if select_node.args.get("where"):
        return True
    return False


def _check_cartesian_product(root: exp.Expression) -> Optional[str]:
    """Verifica se ha produto cartesiano sem condicao (DoS potencial).

    Percorre TODOS os SELECTs da arvore: o de fora, os lados de um UNION, as CTEs e
    as subqueries. Antes so o SELECT externo era verificado, e o mesmo produto
    cartesiano passava escondido num WITH ou num FROM (subquery).
    """
    for select_node in root.find_all(exp.Select):
        source_count = _count_from_sources(select_node)
        if source_count > MAX_UNCONSTRAINED_SOURCES and not _has_join_condition(select_node):
            return (
                f"Produto cartesiano de {source_count} fontes sem condicao de join. "
                "Adicione clausula ON ou WHERE para limitar o resultado."
            )
    return None


def _check_statement_type(root: exp.Expression) -> Optional[str]:
    """Verifica se o statement e SELECT ou WITH (que resolve para SELECT)."""
    if isinstance(root, exp.Select):
        return None
    if isinstance(root, exp.Union):
        return None
    if isinstance(root, exp.Subquery):
        return None
    # Verifica se e CTE que resolve para SELECT
    if hasattr(root, "this"):
        inner = root.this
        if isinstance(inner, (exp.Select, exp.Union)):
            return None
    return f"SQL deve ser SELECT ou WITH. Tipo encontrado: {type(root).__name__}"


# ==============================================================================
# BACKSTOP: Regex-based validation (defense-in-depth)
# ==============================================================================


def _backstop_check(sql: str) -> Optional[str]:
    """Checagem de backstop via regex (caso algo escape o AST)."""
    # Remove literais de string para o scan de keyword
    skeleton = _STRING_LITERAL_RE.sub("''", sql)
    # Remove comentarios: "-- create resumo" num titulo ou "/* drop ... */" num aparte
    # nao sao comandos. Seguro porque os literais ja sairam (um "--" dentro de string
    # nao chega aqui) e porque o AST em is_safe continua sendo a barreira real.
    skeleton = _COMMENT_RE.sub(" ", skeleton)

    # Verifica keywords bloqueadas
    m = _BLOCKED_RE.search(skeleton)
    if m:
        return f"Comando bloqueado: '{m.group(1).upper()}' nao e permitido."

    # Verifica statement stacking
    if ";" in skeleton.strip().rstrip(";"):
        return "Multiplos comandos nao sao permitidos."

    # String dollar-quoted ($$...$$): nenhuma fonte usa, e ela escapa das regex de
    # http/https abaixo (que exigem aspas antes do esquema).
    if "$$" in sql:
        return "String dollar-quoted ($$) nao permitida."

    # Verifica HTTP cleartext / SSRF: http:// SEMPRE bloqueado (IMDS/metadata).
    if _HTTP_CLEARTEXT_RE.search(sql):
        return "URLs http:// (cleartext) bloqueadas. Use s3:// ou https:// das fontes autorizadas."
    # https:// permitido SO para hosts allowlisted (fecha SSRF p/ dominio arbitrario).
    for m in _HTTPS_HOST_RE.finditer(sql):
        if not _https_host_allowed(m.group(1)):
            return (
                f"Host HTTPS '{m.group(1)}' nao autorizado. "
                "Fonte permitida: ONS."
            )

    # Verifica LFI
    if _LOCAL_PATH_LITERAL_RE.search(sql):
        return "Caminho de arquivo local bloqueado. Use apenas URLs s3://."

    return None


# ==============================================================================
# MAIN VALIDATOR CLASS
# ==============================================================================


class SqlValidator:
    """Valida SQL antes de executar no DuckDB.

    Usa sqlglot para parse AST, eliminando bypasses via comentario/quote.
    O argumento `con` e mantido por compatibilidade mas nao e mais usado
    (validacao de sintaxe agora e via parse, nao EXPLAIN).
    """

    def __init__(self, con):
        self.con = con  # Mantido por compatibilidade

    def is_safe(self, sql: str) -> tuple[bool, str]:
        """Valida seguranca via AST + backstop regex."""
        # 1. Backstop primeiro (rapido, pega casos obvios)
        err = _backstop_check(sql)
        if err:
            return False, err

        # 2. Parse com sqlglot (normaliza comentarios e quotes)
        try:
            statements = sqlglot.parse(sql, read="duckdb")
        except Exception as e:
            return False, f"Erro de sintaxe SQL: {str(e)[:200]}"

        # 3. Exatamente 1 statement
        statements = [s for s in statements if s is not None]
        if len(statements) != 1:
            return False, f"Exatamente 1 comando SQL permitido. Encontrados: {len(statements)}"

        root = statements[0]

        # 4. Deve ser SELECT/WITH
        err = _check_statement_type(root)
        if err:
            return False, err

        # 5. Verifica funcoes bloqueadas
        err = _check_blocked_functions(root)
        if err:
            return False, err

        # 6. Verifica tabelas de sistema bloqueadas
        err = _check_blocked_tables(root)
        if err:
            return False, err

        # 7. Verifica leitores fora da allowlist
        err = _check_non_allowed_readers(root)
        if err:
            return False, err

        # 7b. Tabela implicita por caminho/URL (FROM 's3://..', FROM 'x.parquet')
        err = _check_implicit_path_tables(root)
        if err:
            return False, err

        # 8. Verifica bucket S3 allowlist
        err = _check_s3_bucket_allowlist(root)
        if err:
            return False, err

        # 9. Verifica literais gigantes (anti-crash)
        err = _check_giant_literals(root)
        if err:
            return False, err

        # 10. Verifica WITH RECURSIVE
        err = _check_recursive_cte(root)
        if err:
            return False, err

        # 11. Verifica produto cartesiano
        err = _check_cartesian_product(root)
        if err:
            return False, err

        return True, ""

    def is_valid_syntax(self, sql: str) -> tuple[bool, str]:
        """Valida sintaxe SQL.

        Migrado de EXPLAIN para parse-only (evita crash por constant-folding).
        """
        # Pula comentarios INICIAIS ("-- titulo" / "/* ... */") antes do check de
        # prefixo: o LLM prefixa a consulta com titulo e rejeitar custa um
        # round-trip inteiro (boot frio + retry). Seguro: antes do 1o token nao
        # existe literal de string; o AST (is_safe) segue validando o SQL todo.
        body = sql.strip()
        while True:
            if body.startswith("--"):
                nl = body.find("\n")
                body = "" if nl < 0 else body[nl + 1 :].lstrip()
            elif body.startswith("/*"):
                end = body.find("*/")
                if end < 0:
                    break  # comentario nao fechado: deixa o parser reportar
                body = body[end + 2 :].lstrip()
            else:
                break
        sql_upper = body.upper()
        if sql_upper.startswith("DESCRIBE"):
            return True, ""
        if not sql_upper.startswith("SELECT") and not sql_upper.startswith("WITH"):
            return False, f"SQL deve comecar com SELECT ou WITH. Recebido: {sql_upper[:30]}..."

        try:
            statements = sqlglot.parse(sql, read="duckdb")
            if not statements or not statements[0]:
                return False, "SQL vazio ou invalido."
            return True, ""
        except Exception as e:
            return False, str(e)[:200]

    def validate(self, sql: str) -> tuple[bool, str]:
        """Validacao completa: seguranca + sintaxe."""
        ok, msg = self.is_safe(sql)
        if not ok:
            return False, msg
        return self.is_valid_syntax(sql)
