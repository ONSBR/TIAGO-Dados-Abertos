"""Proveniencia estruturada (SSOT): bloco ```tiago-dados-abertos-sources``` nos resultados.

Contrato com o front: `sources` = somente datasets REAIS (validados no catalogo)
com a `portal_url` CANONICA do contrato (authoritativeDefinitions — o slug do
PORTAL); `period` = janela OBSERVADA nas linhas retornadas (min/max da coluna
temporal declarada em row_grain), NUNCA a janela pedida — defasagem de
publicacao tornaria o carimbo mentiroso.

Regra de ouro: falso negativo aceitavel (omitir), falso positivo carimbado
jamais. Toda ambiguidade resolve em OMISSAO: URI compartilhado por dois
contratos, celula temporal invalida, tabela paginada, header duplicado.
"""

import json
import re
from datetime import date, datetime

_S3_ANY_RE = re.compile(r"s3://[^\s'\")]+")
# celula temporal: data ISO, hora opcional, offset de timezone opcional
# (TIMESTAMPTZ do DuckDB: 2024-01-01 12:00:00-03) — validada inteira depois.
_CELULA_TEMPORAL_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})([ T]\d{2}:\d{2}(:\d{2})?(\.\d+)?([+-]\d{2}(:?\d{2})?|Z)?)?$"
)


def bloco_sources(metas: list, period: tuple | None = None) -> str:
    """Bloco cercado e rotulado com as fontes (dedup por dataset) + periodo opcional.

    Fonte sem portal_url e OMITIDA (conformidade de shape com o front).
    """
    vistos: set = set()
    fontes = []
    for m in metas or []:
        nome = (m or {}).get("name")
        url = (m or {}).get("portal_url")
        if not nome or not url or nome in vistos:
            continue
        vistos.add(nome)
        fontes.append({"dataset": nome, "display_name": nome, "portal_url": url})
    if not fontes:
        return ""
    payload: dict = {"sources": fontes}
    if period:
        payload["period"] = {"start": period[0], "end": period[1]}
    return "\n\n```tiago-dados-abertos-sources\n" + json.dumps(payload, ensure_ascii=False) + "\n```"


def temporal_col(meta: dict) -> str | None:
    """Coluna temporal declarada no contrato (row_grain) — fato de schema, estavel."""
    sem = (meta or {}).get("semantics") or {}
    rg = sem.get("row_grain") or {}
    return rg.get("temporal_column") or None


def _raiz_do_contrato(meta: dict) -> str:
    """Diretorio-raiz s3 do dataset. parquet_source pode ser a EXPRESSAO
    read_parquet('s3://...', ...) completa — extrai a URI de dentro."""
    loc = meta.get("parquet_source") or meta.get("s3_location") or ""
    m = _S3_ANY_RE.search(loc)
    if not m:
        return ""
    return m.group(0).split("*")[0].rstrip("/")


_DOLLAR_TAG_RE = re.compile(r"\$[A-Za-z_0-9]*\$")


def _uris_de_leitores(sql: str, leitores: frozenset, esquemas: tuple) -> list:
    """URIs (esquemas) que sao ARGUMENTO REAL de um leitor em `leitores` — lexer lexical de SQL.

    Regex crua sobre o texto aceitaria read_parquet dentro de comentario ou de
    literal, fabricando fonte de uma query que nao leu parquet nenhum. O lexer
    cobre as 5 classes lexicais do SQL (DuckDB/Postgres): string '...' (escape
    ''), identificador "..." (escape ""), string dollar-quoted $tag$...$tag$,
    comentario -- e comentario /* */ ANINHADO. So read_parquet que aparece em
    CODIGO abre a coleta; URIs so como literal de argumento ate fechar o
    parentese. Dollar-string como argumento e OMITIDA (conservador).
    """
    tokens: list = []  # ("code", ch) | ("str", conteudo) | ("opaco", None)
    i, n = 0, len(sql or "")
    while i < n:
        c = sql[i]
        if c == "'":
            # escape-string E'...': backslash escapa (\' NAO fecha o literal).
            # Detecta o prefixo E/e como palavra isolada no codigo ja emitido.
            e_string = bool(
                tokens
                and tokens[-1][0] == "code"
                and tokens[-1][1] in ("E", "e")
                and (
                    len(tokens) < 2
                    or tokens[-2][0] != "code"
                    or not (tokens[-2][1].isalnum() or tokens[-2][1] == "_")
                )
            )
            j, buf = i + 1, []
            while j < n:
                if e_string and sql[j] == "\\" and j + 1 < n:
                    buf.append(sql[j + 1])
                    j += 2
                    continue
                if sql[j] == "'" and j + 1 < n and sql[j + 1] == "'":
                    buf.append("'")
                    j += 2
                    continue
                if sql[j] == "'":
                    break
                buf.append(sql[j])
                j += 1
            tokens.append(("str", "".join(buf)))
            i = j + 1
            continue
        if c == '"':
            # identificador entre aspas (escape ""): NAO e codigo — um alias
            # chamado "read_parquet(" nao pode armar o detector
            j = i + 1
            while j < n:
                if sql[j] == '"' and j + 1 < n and sql[j + 1] == '"':
                    j += 2
                    continue
                if sql[j] == '"':
                    break
                j += 1
            tokens.append(("opaco", None))
            i = j + 1
            continue
        if c == "$":
            m = _DOLLAR_TAG_RE.match(sql, i)
            if m:
                fecho = m.group(0)
                fim = sql.find(fecho, m.end())
                tokens.append(("opaco", None))  # conteudo inteiro fora do jogo
                i = n if fim == -1 else fim + len(fecho)
                continue
        if sql[i : i + 2] == "--":
            fim = sql.find("\n", i)
            i = n if fim == -1 else fim + 1
            continue
        if sql[i : i + 2] == "/*":
            # comentario de bloco ANINHADO (estilo Postgres/DuckDB)
            depth, j = 1, i + 2
            while j < n and depth:
                if sql[j : j + 2] == "/*":
                    depth += 1
                    j += 2
                elif sql[j : j + 2] == "*/":
                    depth -= 1
                    j += 2
                else:
                    j += 1
            i = j
            continue
        tokens.append(("code", c))
        i += 1

    uris: list = []
    profundidade = 0  # >0 = dentro dos argumentos de um read_parquet
    palavra = ""  # identificador em construcao (fronteira de palavra REAL)
    aguarda_paren = False  # vi read_parquet, espero '(' (so whitespace no meio)

    def _reset():
        nonlocal palavra, aguarda_paren
        palavra, aguarda_paren = "", False

    for tipo, valor in tokens:
        if tipo == "str":
            if profundidade > 0 and valor.startswith(esquemas):
                uris.append(valor)
            _reset()  # string em posicao de codigo zera o estado do identificador
            continue
        if tipo == "opaco":
            _reset()  # identificador "..."/dollar nao pode armar o detector
            continue
        if profundidade > 0:
            if valor == "(":
                profundidade += 1
            elif valor == ")":
                profundidade -= 1
            continue

        ch = valor
        if ch.isalnum() or ch == "_":
            palavra += ch
            aguarda_paren = False  # char apos read_parquet(sem '(') cancela (ex.: read_parquetX)
            continue
        # fronteira: finaliza a palavra corrente
        if palavra:
            aguarda_paren = palavra.lower() in leitores  # match EXATO, nao sufixo
            palavra = ""
        if ch.isspace():
            continue  # 'read_parquet   (' segue valido
        if ch == "(" and aguarda_paren:
            profundidade = 1
        aguarda_paren = False  # qualquer outra pontuacao cancela
    return uris


_LEITORES_S3 = frozenset({"read_parquet"})
_LEITORES_HTTP = frozenset({"read_csv", "read_csv_auto", "read_json", "read_json_auto", "read_parquet"})


def _uris_de_read_parquet(sql: str) -> list:
    """Compat: URIs s3 de read_parquet(...) — comportamento original, intacto."""
    return _uris_de_leitores(sql, _LEITORES_S3, ("s3://",))


def _urls_http_de_leitores(sql: str) -> list:
    """URLs http(s) que sao argumento REAL de read_csv/read_json/read_parquet.

    Necessario para dataset servido por https (read_csv/read_json): antes so s3://
    era lexado, e nesses o bloco de fontes/computed-facts nunca disparava."""
    return _uris_de_leitores(sql, _LEITORES_HTTP, ("http://", "https://"))


_URL_NA_FONTE_RE = re.compile(r"'(https?://[^']+)'")


def _casa_fonte_templada(url: str, meta) -> bool:
    """A URL executada casa uma fonte do contrato que declara placeholder ({data_arquivo})?
    Compara prefixo E sufixo em volta do placeholder: sufixo solto casaria datasets irmaos."""
    fonte = str((meta or {}).get("parquet_source") or "")
    if "{" not in fonte:
        return False
    for declarada in _URL_NA_FONTE_RE.findall(fonte):
        if "{" not in declarada:
            continue
        padrao = "".join(
            ".+" if p.startswith("{") else re.escape(p)
            for p in re.split(r"(\{[a-z_]+\})", declarada) if p
        )
        if re.fullmatch(padrao, url):
            return True
    return False


def datasets_do_sql(sql: str, catalog: dict) -> list:
    """Datasets efetivamente LIDOS no SQL.

    S3 (read_parquet): URI -> raiz do contrato, match por FRONTEIRA de diretorio
    (raiz + '/'): taxa_teif_teip nao pode casar com taxa_teif_teip_oper.
    HTTP(S) (read_csv/read_json): URL EXATA como literal entre aspas dentro do
    sourceExpression do contrato (o id do recurso e unico; substring casaria
    prefixo). Em ambos, URI/URL em 2+ contratos e AMBIGUA e nao contribui —
    omissao antes de chute. URL efemera ou parametrizada em runtime nao casa =
    omissao aceitavel (falso negativo).
    """
    paths = _uris_de_read_parquet(sql or "")
    urls = _urls_http_de_leitores(sql or "")
    if not paths and not urls:
        return []

    por_nome: dict = {}
    if paths:
        raizes = []
        for meta in catalog.values():
            raiz = _raiz_do_contrato(meta)
            if raiz:
                raizes.append((raiz, meta))
        for p in paths:
            candidatos = [(len(r), m) for r, m in raizes if p == r or p.startswith(r + "/")]
            if not candidatos:
                continue
            maior = max(tam for tam, _ in candidatos)
            top = [m for tam, m in candidatos if tam == maior]
            if len(top) != 1:
                continue  # mesmo URI em 2+ contratos: ambiguo => omite
            por_nome[top[0]["name"]] = top[0]

    for u in urls:
        alvo = f"'{u}'"
        donos = [m for m in catalog.values() if alvo in str((m or {}).get("parquet_source") or "")]
        if not donos:
            # fonte TEMPLADA (um arquivo por dia: .../focos_diario_br_{data_arquivo}.csv):
            # a URL executada tem a data concreta; casa pelo prefixo ate o placeholder e
            # pelo sufixo depois dele — nunca por prefixo solto (isso casaria irmaos).
            donos = [m for m in catalog.values() if _casa_fonte_templada(u, m)]
        if len(donos) == 1:
            por_nome[donos[0]["name"]] = donos[0]
    return list(por_nome.values())


def _celulas_md(ln: str) -> list | None:
    """Celulas de uma linha markdown. split SEM strip('|') generoso: '||' extra
    no fim vira celula fantasma e reprova; linha fora do shape '| a | b |' idem."""
    partes = ln.split("|")
    if len(partes) < 3 or partes[0].strip() or partes[-1].strip():
        return None
    return [p.strip() for p in partes[1:-1]]


def _min_max_iso_da_coluna(linhas: list, header: list, idx: int) -> tuple | None:
    """min/max ISO da coluna idx; None se QUALQUER celula nao for data ISO valida."""
    datas = []
    for ln in linhas[2:]:
        cels = _celulas_md(ln)
        if cels is None or len(cels) != len(header):
            return None  # celula com '|' desloca colunas: linhagem indeterminavel
        cel = cels[idx]
        m = _CELULA_TEMPORAL_RE.match(cel)
        if not m:
            return None  # UMA celula invalida veta a coluna inteira
        try:
            # valida a CELULA INTEIRA (data e hora): 2024-99-99 e 99:99:99 reprovam
            if len(cel) > 10:
                datetime.fromisoformat(cel.replace(" ", "T"))
            else:
                date.fromisoformat(cel)
        except ValueError:
            return None
        datas.append(m.group(1))
    return (min(datas), max(datas)) if datas else None


def periodo_observado_md(markdown: str, coluna: str | None, auto_detect: bool = False) -> tuple | None:
    """(start, end) = min/max OBSERVADOS na coluna temporal da tabela markdown.

    Le a PROPRIA tabela renderizada (vale ate para resultado vindo do cache).
    ESTRITO — omite quando: paginado (janela parcial mentiria); linha com nº de
    celulas != header; QUALQUER celula temporal invalida (mistura ISO/nao-ISO,
    data/hora impossivel, sufixo estranho).

    coluna = nome esperado (row_grain.temporal_column). Quando ela nao esta no
    header E auto_detect=True (SO no caminho de template CONFIAVEL do motor
    deterministico — o motor projeta a coluna com alias, ex.: DATE_TRUNC(...) AS mes),
    procura a UNICA coluna 100%-ISO do resultado. Ambiguo (0 ou 2+) => omite.
    auto_detect NUNCA em SQL de usuario: la um alias fabricaria a coluna.
    """
    if not markdown or "*[Paginacao:" in markdown:
        return None
    linhas = [ln for ln in markdown.split("\n") if ln.startswith("|")]
    if len(linhas) < 3:
        return None
    header_cru = _celulas_md(linhas[0])
    if header_cru is None:
        return None
    header = [h.strip("`").strip() for h in header_cru]

    if coluna and header.count(coluna) == 1:
        return _min_max_iso_da_coluna(linhas, header, header.index(coluna))
    if not auto_detect:
        return None
    # template confiavel: acha a UNICA coluna toda-ISO (a temporal projetada c/ alias)
    achadas = [(i, _min_max_iso_da_coluna(linhas, header, i)) for i in range(len(header))]
    validas = [(i, r) for i, r in achadas if r is not None]
    return validas[0][1] if len(validas) == 1 else None


# ── Rota do SQL: governado (sql_pattern do corpus) vs ad-hoc ─────────────────
# Metrica de qualidade: share de consultas que passam pela camada semantica. Aqui a
# camada governada = sql_pattern validado. Caixa, espacos, ';' e LIMIT/OFFSET finais
# nao contam.
_WS_RE = re.compile(r"\s+")
_TAIL_LIMIT_RE = re.compile(r"(\s+limit\s+\d+(\s+offset\s+\d+)?)?\s*;?\s*$", re.I)
_rota_idx_cache: dict = {}


def _normalize_sql(sql: str) -> str:
    s = _WS_RE.sub(" ", sql or "").strip().lower()
    return _TAIL_LIMIT_RE.sub("", s).strip()


def _indice_patterns(catalog: dict) -> dict:
    """normalize(pattern renderizado com {source}) -> 'dataset/pattern'. Cache
    por identidade do catalogo (reload cria dict novo => reindexa)."""
    key = (id(catalog), len(catalog))
    idx = _rota_idx_cache.get(key)
    if idx is not None:
        return idx
    idx = {}
    regexes = []  # templates COM placeholders ({year}, {subsistema}...): casam por regex
    for meta in catalog.values():
        src = str((meta or {}).get("parquet_source") or "")
        pats = ((meta or {}).get("semantics") or {}).get("sql_patterns") or {}
        if not src or not isinstance(pats, dict):
            continue
        for pname, spec in pats.items():
            tmpl = spec.get("template") if isinstance(spec, dict) else spec
            if not isinstance(tmpl, str) or "{source}" not in tmpl:
                continue
            norm = _normalize_sql(tmpl.replace("{source}", src))
            rota = f"{meta.get('name')}/{pname}"
            if _PLACEHOLDER_RE.search(norm):
                partes = [re.escape(p) for p in _PLACEHOLDER_RE.split(norm)]
                try:
                    regexes.append((re.compile("^" + _PARAM_VALOR_RE.join(partes) + "$", re.S), rota))
                except re.error:
                    continue
            else:
                idx[norm] = rota
    idx["\x00regexes"] = regexes
    _rota_idx_cache.clear()
    _rota_idx_cache[key] = idx
    return idx


# Placeholder de sql_pattern ({year}, {subsistema}, {start}...) e o VALOR que o
# preenche no SQL renderizado — SO formas de LITERAL: string entre aspas, numero
# (com sinal/decimal), data ISO (com prefixo DATE/TIMESTAMP opcional) ou um
# identificador simples (current_date). Revisao: "[^\s,()']+"
# aceitava `2011/**/OR/**/1=1` (comentario embutido muda a semantica sem quebrar o
# regex) e tratava como rota por pattern um SQL adulterado.
# Nunca engole clausulas inteiras (o resto do template casa caractere a caractere).
_PLACEHOLDER_RE = re.compile(r"\{[a-z_][a-z0-9_]*\}")
_PARAM_VALOR_RE = (
    r"(?:'[^']*'"                                   # 'SE', '2025-01-01'
    r"|(?:date|timestamp)\s+'[^']*'"                # DATE '2025-01-01'
    r"|-?\d+(?:\.\d+)?"                             # 2025, -3.5
    r"|\d{4}-\d{2}-\d{2}(?:[ t]\d{2}:\d{2}(?::\d{2})?)?"  # 2025-01-01[ 00:00[:00]]
    r"|[a-z_][a-z0-9_]{0,40})"                      # current_date, sin
)


def rota_do_sql(sql, catalog: dict) -> tuple:
    """('pattern', 'dataset/nome') se o SQL bate com um sql_pattern renderizado do
    corpus; senao ('adhoc', None). Nunca lanca."""
    try:
        if not sql or not catalog:
            return ("adhoc", None)
        idx = _indice_patterns(catalog)
        chave = _normalize_sql(sql)
        if chave in idx:
            return ("pattern", idx[chave])
        for rx, rota in idx.get("\x00regexes") or []:
            if rx.match(chave):
                return ("pattern", rota)
        return ("adhoc", None)
    except Exception:
        return ("adhoc", None)


def rodape_participacao_multi(by_rows, max_linhas: int = 12) -> str:
    """Rodape textual (o LLM le texto) quando ha >=2 medidas — o footer do db
    (db._participacao_footer) so cobre exatamente 1 medida, e nunca duplicar.
    Mesmo prefixo '*[participacao sobre o total das linhas' que o prompt ja
    manda usar. Medida sem pct (negativos) ou com >max_linhas grupos e omitida."""
    try:
        meds = (by_rows or {}).get("measures") or {}
        if len(meds) < 2:
            return ""
        partes = []
        for col, m in meds.items():
            gs = m.get("groups") or []
            if not gs or len(gs) > max_linhas or any("pct" not in g for g in gs):
                continue
            partes.append(f"{col}: " + ", ".join(f"{g['label']}: {g['pct']:.1f}%" for g in gs))
        if len(partes) < 2:
            return ""
        return "\n\n*[participacao sobre o total das linhas — " + "; ".join(partes) + "]*"
    except Exception:
        return ""
