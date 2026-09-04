# -*- coding: utf-8 -*-
"""Poda de arquivos parquet por ano (partition pruning manual) + filtro temporal sargavel.

Motivacao: o engine lia o glob '.../*.parquet' inteiro (todos os anos) + EXTRACT(YEAR)=Y.
Bench: poda de arquivos = 2-9x mais rapido (geracao-usina 89s->9,6s).

SEGURANCA (por que nao quebra) — NAO ha whitelist hardcoded (classe-nao-fatos):
- Poda SO em datasets cujo CONTRATO tem `partitionPruning: year` no customProperties. Essa flag e
  escrita APOS validar que os arquivos do ano contem SO aquele ano E COUNT(podado)==COUNT(glob)
  (cvu-usitermica reprovou por cross-year leak). Dataset novo/particionamento novo: re-rodar o
  validador+patcher -> o contrato ganha (ou perde) a flag.
- A prova de suficiencia dos anos vem SO de conjunctos AND do WHERE do SELECT que le o glob,
  via AST (sqlglot). Sem prova / multi-glob / nome sem ano / falha ao listar ou parsear
  -> retorna o glob original (fallback seguro).
- O filtro WHERE do ano continua aplicado; a poda so evita ABRIR arquivos de outros anos.
"""

import re
import time

import boto3
import sqlglot
from botocore import UNSIGNED
from botocore.config import Config
from sqlglot import exp

# IGNORECASE: o engine renderiza READ_PARQUET MAIUSCULO nos sql_patterns e o LLM
# tambem escreve maiusculo; sem isto a poda (prune_sql/prune_user_sql) nunca casava
# e caia no glob de todos os anos (query ~30x mais lenta).
_GLOB_RE = re.compile(r"read_parquet\(\s*'s3://([^/]+)/(.+?/)\*\.parquet'\s*(,[^)]*)?\)", re.IGNORECASE)
_YEAR_RE = re.compile(r"(?<!\d)(20\d{2})(?!\d)")
_TTL = 600  # s — re-lista a cada 10min (pega arquivo novo de datasets diario/mensal sem ficar velho)
_cache: dict[str, tuple[float, list[str]]] = {}
_s3 = None


def _s3c():
    global _s3
    if _s3 is None:  # anonimo: bucket ONS e publico (mesma via dos reads httpfs)
        _s3 = boto3.client("s3", region_name="us-west-2", config=Config(signature_version=UNSIGNED))
    return _s3


def _list(bucket: str, prefix: str) -> list[str]:
    now = time.time()
    hit = _cache.get(prefix)
    if hit and hit[0] > now:
        return hit[1]
    keys, tok = [], None
    while True:
        kw = {"Bucket": bucket, "Prefix": prefix, "MaxKeys": 1000}
        if tok:
            kw["ContinuationToken"] = tok
        r = _s3c().list_objects_v2(**kw)
        keys += [o["Key"] for o in r.get("Contents", []) if o["Key"].endswith(".parquet")]
        if not r.get("IsTruncated"):
            break
        tok = r["NextContinuationToken"]
    _cache[prefix] = (now + _TTL, keys)
    return keys


def year_range_filter(time_col: str, year) -> str:
    """Filtro temporal SARGAVEL (habilita row-group pruning) — equivalente a EXTRACT(YEAR)=Y."""
    return (
        f"TRY_CAST({time_col} AS TIMESTAMP) >= TIMESTAMP '{int(year)}-01-01' "
        f"AND TRY_CAST({time_col} AS TIMESTAMP) < TIMESTAMP '{int(year) + 1}-01-01'"
    )


def prune_source(parquet_source: str, prune_scheme: str, year) -> str:
    """Reescreve o glob '.../*.parquet' p/ a lista de arquivos do ano, SE o contrato marca
    partitionPruning=year (prune_scheme) e ha um ano. Senao, retorna o glob original (fallback seguro)."""
    if not parquet_source or year is None or prune_scheme != "year":
        return parquet_source
    try:
        y = int(year)
    except (TypeError, ValueError):
        return parquet_source
    m = _GLOB_RE.search(parquet_source)
    if not m:
        return parquet_source
    return _prune_glob_match(m, {y}) or parquet_source


def _prune_glob_match(m: "re.Match", yset: set[int]) -> str | None:
    """Reescreve o glob casado em `m` p/ a lista de arquivos dos anos em `yset`.
    None = sem ganho ou falha (chamador mantem o original)."""
    bucket, prefix, tail = m.group(1), m.group(2), m.group(3) or ", union_by_name=true"
    try:
        files = _list(bucket, prefix)
    except Exception:
        return None  # falha ao listar -> glob (seguro)
    yk, found_years = [], set()
    for k in files:
        mm = _YEAR_RE.search(k.rsplit("/", 1)[-1])
        if mm and int(mm.group(1)) in yset:
            yk.append(k)
            found_years.add(int(mm.group(1)))
    if found_years != yset:  # ano do span sem arquivo: listagem defasada (cache 10min)
        return None  # ou ano futuro -> glob (nao esconder arquivo recem-publicado)
    if not yk or len(yk) == len(files):  # nenhum dos anos OU todos batem -> sem ganho
        return None
    lst = ", ".join(f"'s3://{bucket}/{k}'" for k in yk)
    return f"read_parquet([{lst}]{tail})"


def prune_sql(sql: str, prune_scheme: str, year) -> str:
    """Poda o glob num SQL renderizado de sql_pattern, SO se seguro (usado no pattern-path):
    - contrato marca partitionPruning=year (prune_scheme) e ha um ano;
    - o ano aparece no SQL (ha filtro temporal p/ casar a poda — senao podar restringiria dado a mais);
    - existe EXATAMENTE UM read_parquet glob. 2+ = subquery tipo `MAX(data) FROM read_parquet(...)`;
      podar o glob dela daria o MAX do ANO, nao o global -> NAO poda (deixa o pattern no glob).
    Qualquer condicao falha -> retorna o SQL original (fallback seguro)."""
    if not sql or year is None or prune_scheme != "year":
        return sql
    try:
        y = int(year)
    except (TypeError, ValueError):
        return sql
    if str(y) not in sql:  # sem o ano no SQL -> nao ha filtro temporal casando a poda
        return sql
    matches = list(_GLOB_RE.finditer(sql))
    if len(matches) != 1:  # 0 = nada a podar; 2+ = subquery (MAX) -> nao podar
        return sql
    m = matches[0]
    pruned = prune_source(m.group(0), prune_scheme, y)
    if pruned == m.group(0):
        return sql
    return sql[: m.start()] + pruned + sql[m.end() :]


# Analise por AST (sqlglot, mesmo parser do validator/semantics_engine): a poda so
# confia em predicados que sao CONJUNCTOS AND do WHERE do SELECT que le o glob.
# Predicados em FILTER(WHERE)/CASE/COUNT_IF/QUALIFY/HAVING/JOIN ON, ramos OR/NOT,
# subqueries e comentarios NUNCA entram na prova — eles nao restringem a varredura
# (analise textual/regex podava errado com FILTER, COUNT_IF, (range) IS FALSE,
# QUALIFY, range em coluna nao-temporal e ate range dentro de comentario).
# Conjuncto AND apenas RESTRINGE linhas: provar por ele e seguro por construcao,
# e o resto da query pode ter o que quiser.
# Literal de data SEM timezone: offset (+03/-03/Z) desloca a fronteira de ano
# ('2025-01-01 00:00:00+14' e 2024 em UTC) -> literal com tz nao entra na prova.
_DATE_LIT_RE = re.compile(r"^(20\d{2})-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2}(\.\d+)?)?)?$")
_MIDNIGHT_RE = re.compile(r"^([ T]00:00(:00(\.0+)?)?)?$")
_YEAR_ONLY_RE = re.compile(r"^20\d{2}$")
_RANGE_FLIP = {exp.GT: exp.LT, exp.GTE: exp.LTE, exp.LT: exp.GT, exp.LTE: exp.GTE}


def _unwrap(node):
    """Remove CAST/TRY_CAST/parenteses (idioma sargavel TRY_CAST(col AS TIMESTAMP))."""
    while isinstance(node, (exp.Cast, exp.TryCast, exp.Paren)):
        node = node.this
    return node


def _date_lit(node):
    """(ano, limite_e_1jan_meia_noite) p/ literal 'YYYY-MM-DD[ hh:mm:ss]'; None se nao e.
    CAST para tipo com timezone (TIMESTAMPTZ / WITH TIME ZONE) -> None (fronteira ambigua)."""
    while isinstance(node, (exp.Cast, exp.TryCast, exp.Paren)):
        to = node.args.get("to")
        if to is not None:
            t = to.sql().upper()
            # TIMESTAMPTZ/LTZ ou WITH TIME ZONE = tz-aware; NTZ/WITHOUT = naive (ok)
            if ("TZ" in t and "NTZ" not in t) or ("TIME ZONE" in t and "WITHOUT" not in t):
                return None
        node = node.this
    if not (isinstance(node, exp.Literal) and node.is_string):
        return None
    s = node.this
    if not _DATE_LIT_RE.match(s):
        return None
    return int(s[:4]), s[4:10] == "-01-01" and bool(_MIDNIGHT_RE.match(s[10:]))


def _bare_year(node):
    """Ano p/ literal que E exatamente um ano (2024 ou '2024'); None se nao e."""
    node = _unwrap(node)
    if isinstance(node, exp.Literal) and _YEAR_ONLY_RE.match(str(node.this)):
        return int(node.this)
    return None


def _conjuncts(node):
    """Achata a arvore de ANDs do WHERE; qualquer outro no (OR/NOT/IS/...) sai inteiro."""
    stack = [node]
    while stack:
        n = stack.pop()
        if isinstance(n, exp.And):
            stack += [n.this, n.expression]
        elif isinstance(n, exp.Paren):
            stack.append(n.this)
        else:
            yield n


def _years_to_prune(sql: str, temporal_col: str | None) -> set[int] | None:
    """Anos PROVADAMENTE suficientes p/ responder o SQL, ou None (nao podar).

    Prova SO por conjunctos AND do WHERE do SELECT que le o read_parquet:
    - Range FECHADO na coluna temporal (>=/> com <=/<, ou BETWEEN): poda o span;
      `< 'Y-01-01'` (meia-noite exata) exclui Y, com hora no literal inclui Y.
    - Range ABERTO (so piso OU so teto): NAO poda — linhas de outros anos qualificam.
    - Igualdade pontual: literal-ano puro (ano=Y, EXTRACT(YEAR)=Y, strftime='Y') em
      expressao nao-literal, ou data exata na coluna temporal.
    - Literal-ano em QUALQUER outro lugar da query (fora dos conjunctos da prova):
      NAO poda — nao sabemos o papel dele (conservador).
    Parse falhou / sem WHERE / glob ausente ou repetido -> None (fallback seguro).
    """
    try:
        tree = sqlglot.parse_one(sql, read="duckdb")
    except Exception:
        return None
    rp = list(tree.find_all(exp.ReadParquet))  # duckdb dialect: no dedicado
    rp += [f for f in tree.find_all(exp.Anonymous) if (f.name or "").lower() == "read_parquet"]
    if len(rp) != 1:
        return None
    sel = rp[0].find_ancestor(exp.Select)
    if sel is None:
        return None
    where = sel.args.get("where")
    if where is None:
        return None
    # Binding de coluna ao glob: com JOIN no SELECT, coluna sem qualificador
    # e ambigua e qualificador de OUTRA relacao nao restringe o parquet -> so aceita
    # coluna nua sem JOIN, ou qualificada com o proprio alias do read_parquet.
    tbl = rp[0].find_ancestor(exp.Table)
    glob_alias = (tbl.alias or "").lower() if tbl is not None else ""
    has_joins = bool(sel.args.get("joins"))

    def _bound(col: exp.Column) -> bool:
        q = (col.table or "").lower()
        if q:
            return q == glob_alias and glob_alias != ""
        return not has_joins

    def _temporal(node) -> bool:
        node = _unwrap(node)
        return (
            temporal_col is not None
            and isinstance(node, exp.Column)
            and node.name.lower() == temporal_col
            and _bound(node)
        )

    def _eq_expr_ok(expr_side) -> bool:
        """Lado nao-literal de `expr = ANO` / `expr IN (ANOS)`: A COLUNA TEMPORAL nua
        (contrato com temporal_column=ano) ou EXTRATOR DE ANO conhecido no TOPO da
        expressao sobre ela. Coluna de nome qualquer NAO prova (id_usina=2025 nao e
        filtro temporal); aritmetica (YEAR(din)+1=2025 seleciona 2024!), unidade
        nao-ano e expressao sem coluna (COALESCE(NULL,2025)=2025) tampouco. Contrato
        sem temporal_column -> fail-closed (nao ha contra o que provar)."""
        inner = _unwrap(expr_side)
        if isinstance(inner, exp.Column):
            return temporal_col is not None and inner.name.lower() == temporal_col and _bound(inner)
        col = None
        if isinstance(inner, exp.Year):
            col = _unwrap(inner.this)
        elif isinstance(inner, exp.Extract):
            if (inner.this.name or "").upper() == "YEAR":
                col = _unwrap(inner.expression)
        elif isinstance(inner, exp.TimeToStr):  # strftime(col, '%Y')
            fmt = inner.args.get("format")
            if isinstance(fmt, exp.Literal) and fmt.this == "%Y":
                col = _unwrap(inner.this)
        elif isinstance(inner, exp.Anonymous) and (inner.name or "").lower() in ("date_part", "datepart"):
            fargs = inner.expressions
            if len(fargs) == 2 and isinstance(fargs[0], exp.Literal) and str(fargs[0].this).lower() == "year":
                col = _unwrap(fargs[1])
        if not isinstance(col, exp.Column):
            return False
        if temporal_col is None or col.name.lower() != temporal_col:
            return False  # extrator sobre coluna qualquer/sem contrato nao prova
        return _bound(col)

    lo_years: list[int] = []
    hi_effs: list[int] = []
    eq_years: set[int] = set()
    proof_years: set[int] = set()
    for c in _conjuncts(where.this):
        if isinstance(c, exp.Between):
            if _temporal(c.this):
                lo, hi = _date_lit(c.args.get("low")), _date_lit(c.args.get("high"))
                if lo and hi:
                    lo_years.append(lo[0])
                    hi_effs.append(hi[0])
                    proof_years |= {lo[0], hi[0]}
        elif isinstance(c, (exp.GT, exp.GTE, exp.LT, exp.LTE)):
            for col_side, lit_side, op in (
                (c.this, c.expression, type(c)),
                (c.expression, c.this, _RANGE_FLIP[type(c)]),
            ):
                if _temporal(col_side):
                    d = _date_lit(lit_side)
                    if d:
                        y, jan1_mid = d
                        if op in (exp.GT, exp.GTE):
                            lo_years.append(y)
                        else:
                            hi_effs.append(y - 1 if op is exp.LT and jan1_mid else y)
                        proof_years.add(y)
                    break
        elif isinstance(c, exp.EQ):
            for expr_side, lit_side in ((c.this, c.expression), (c.expression, c.this)):
                y = _bare_year(lit_side)
                if y is not None and _eq_expr_ok(expr_side):
                    eq_years.add(y)
                    proof_years.add(y)
                    break
                d = _date_lit(lit_side)
                if d and _temporal(expr_side):
                    eq_years.add(d[0])
                    proof_years.add(d[0])
                    break
        elif isinstance(c, exp.In):
            values = c.args.get("expressions") or []
            ys = [_bare_year(v) for v in values]
            if values and all(y is not None for y in ys) and _eq_expr_ok(c.this):
                eq_years |= set(ys)
                proof_years |= set(ys)
    # Range fechado tem prioridade: eq NAO pode ESTREITAR um range provado (`versao
    # = 2025` junto de range 2024-2025 derrubaria 2024). Range ABERTO (so piso OU so
    # teto) veta a poda mesmo com eq presente — o range exige anos que a eq nao
    # cobre (`din >= '2025-01-01' AND versao = 2026` podaria so 2026).
    if lo_years and hi_effs:
        lo_y, hi_y = max(lo_years), min(hi_effs)  # conjunctos AND: vale o mais apertado
        if hi_y < lo_y:
            return None
        span = set(range(lo_y, hi_y + 1))
    elif lo_years or hi_effs:
        return None  # range aberto: eq nao anula a necessidade dos demais anos
    elif eq_years:
        span = set(eq_years)
    else:
        return None  # sem prova
    # Cinto de seguranca: literal-ano em qualquer no da AST fora da prova -> nao poda.
    all_years: set[int] = set()
    for lit in tree.find_all(exp.Literal):
        all_years |= {int(y) for y in _YEAR_RE.findall(str(lit.this))}
    if all_years - (span | proof_years):
        return None
    return span


def prune_user_sql(sql: str, catalog: dict) -> str:
    """Poda o glob de SQL ESCRITO PELO USUARIO/LLM (executar_sql), nao so o do
    pattern-path. O modelo escreve `read_parquet('.../*.parquet')` sem poda -> varre
    todos os anos (2000-2026) p/ filtrar 1 ano = ~30x mais lento. Aqui: se ha UM unico
    glob, os anos sao PROVADAMENTE suficientes (_years_to_prune: ano pontual ou range
    fechado em conjuncto AND do WHERE, ex. >= '2025-01-01' AND < '2026-01-01'), e o dataset declara
    partitionPruning=year no contrato, reescreve p/ os arquivos desses anos. Fallback
    seguro (SQL intacto) em qualquer duvida: multi-glob (JOIN/subquery), range aberto,
    ou dataset sem a flag (ex.: geracao-termica, removido por cross-year leak)."""
    if not sql:
        return sql
    matches = list(_GLOB_RE.finditer(sql))
    if len(matches) != 1:  # 0 nada; 2+ = subquery/JOIN -> nao poda
        return sql
    m = matches[0]
    # dataset desse glob -> prune_partition + coluna temporal do contrato
    prefix = m.group(2)  # ex.: 'dataset/carga_energia_di/'
    ds_meta = None
    for meta in catalog.values():
        src = f"{meta.get('parquet_source') or ''} {meta.get('s3_location') or ''}"
        if prefix in src:
            ds_meta = meta
            break
    if not ds_meta or ds_meta.get("prune_partition") != "year":
        return sql
    temporal = ((ds_meta.get("semantics") or {}).get("row_grain") or {}).get("temporal_column")
    yset = _years_to_prune(sql, temporal.lower() if temporal else None)
    if not yset:
        return sql
    pruned = _prune_glob_match(m, yset)
    if not pruned:
        return sql
    return sql[: m.start()] + pruned + sql[m.end() :]
