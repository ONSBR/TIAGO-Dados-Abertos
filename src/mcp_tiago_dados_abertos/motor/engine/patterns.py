# -*- coding: utf-8 -*-
"""Renderizacao dos sql_patterns do contrato, fonte templada por periodo, recencia e entidade.

Parte do motor semantico; fatiado de semantics_engine.py pelas suas proprias secoes.
"""
from __future__ import annotations

import datetime as _dt
import re
from datetime import datetime
from typing import Optional

from .normalizacao import _norm, _stem

# ── Renderizacao de sql_patterns ──────────────────────────────────────────────

# Chaves de parametro TEMPORAIS do mapping generico: nunca preenchidas a partir
# de validated_params (ver _valor_param em _fill_params).
_TEMPORAL_PARAM_KEYS = frozenset(
    {
        "year",
        "year_filter",
        "year_start",
        "year_end",
        "month",
        "data_inicio",
        "data_fim",
        "start_date",
        "end_date",
        "start",
        "end",
        "data",
        # aliases do corpus
        "date",
        "data_ref",
        "ano_inicio",
        "ano_fim",
    }
)

# Chaves temporais FORA do mapping generico (placeholders proprios de pattern):
# nomes com segmento temporal nao podem resolver de validated_params no fallback.
_TEMPORAL_KEY_NAME_RE = re.compile(
    r"(?:^|_)(date|data|dia|mes|month|ano|year|competencia|periodo|inicio|fim|start|end)(?:_|$)"
)


FONTE_POR_DIA = "{data_arquivo}"
FONTE_POR_MES = "{mes_arquivo}"
_MAX_ARQUIVOS_MES = 24  # teto: janela maior que 2 anos vira leitura absurda -> recusa


def meses_da_pergunta(params: dict) -> Optional[list]:
    """['YYYYMM', ...] cobertos pela pergunta, para fonte com UM ARQUIVO POR MES.

    Os `focos_mensal_*` do INPE tinham o mes CONGELADO (202401) e respondiam jan/2024 a
    qualquer pergunta. Aqui: mes/ano da pergunta viram a lista real de arquivos (DuckDB le
    lista); sem nada temporal, o mes CORRENTE (o ultimo publicado). None quando a janela passa
    do teto — recusar e melhor que ler 300 arquivos ou responder 1 mes calado.
    """
    def _ym(chave):
        m = re.match(r"(\d{4})-(\d{2})", str(params.get(chave) or ""))
        return (int(m.group(1)), int(m.group(2))) if m else None

    ini = _ym("data_inicio") or _ym("data")
    fim = _ym("data_fim") or _ym("end_date") or _ym("end") or _ym("data") or ini
    if ini is None:
        ano, mes = params.get("year"), params.get("month")
        if ano and mes:
            ini = fim = (int(ano), int(mes))
        elif ano:
            ini, fim = (int(ano), 1), (int(ano), 12)
        else:
            hoje = _dt.date.today()
            ini = fim = (hoje.year, hoje.month)
    total = (fim[0] - ini[0]) * 12 + (fim[1] - ini[1]) + 1
    if total < 1 or total > _MAX_ARQUIVOS_MES:
        return None
    out, (ano, mes) = [], ini
    for _ in range(total):
        out.append(f"{ano:04d}{mes:02d}")
        mes += 1
        if mes > 12:
            ano, mes = ano + 1, 1
    return out


def dia_unico_da_pergunta(params: dict) -> Optional[str]:
    """YYYYMMDD quando a pergunta determina UM UNICO dia; None quando pede um PERIODO.

    Fonte com um arquivo por dia (INPE focos diario) so responde um dia. Sem esta distincao,
    "focos em agosto de 2026" respondia com o arquivo de ONTEM: um dia vendido como o mes
. Sem nada temporal na pergunta = o dia mais recente publicado (D-1).
    Nunca vem de validated_params: foi isso que congelou o dataset em 04/07.
    """
    def _dia(chave):
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(params.get(chave) or ""))
        return "".join(m.groups()) if m else None

    ini, fim, data = _dia("data_inicio"), _dia("data_fim") or _dia("end_date") or _dia("end"), _dia("data")
    if data and (ini is None or ini == data) and (fim is None or fim == data):
        return data
    if ini and fim and ini == fim:
        return ini
    if ini or fim or data:
        return None  # janela de varios dias
    if params.get("year") or params.get("month"):
        return None  # periodo (mes/ano) nao cabe num arquivo de um dia
    return (_dt.date.today() - _dt.timedelta(days=1)).strftime("%Y%m%d")


def _fonte_dos_meses(source: str, params: dict) -> Optional[str]:
    """Fonte com {mes_arquivo} -> URL do mes pedido; varios meses viram LISTA de URLs
    (`read_csv(['..._202606.csv', '..._202607.csv'])`), que o DuckDB le como um so relacao."""
    meses = meses_da_pergunta(params)
    if not meses:
        return None
    if len(meses) == 1:
        return source.replace(FONTE_POR_MES, meses[0])
    m = re.search(r"'([^']*\{mes_arquivo\}[^']*)'", source)
    if not m:
        return None
    lista = ", ".join("'" + m.group(1).replace(FONTE_POR_MES, mes) + "'" for mes in meses)
    return source[:m.start()] + "[" + lista + "]" + source[m.end():]


def resolver_fonte(source: str, params: dict) -> Optional[str]:
    """Fonte templada -> URL(s) do periodo PEDIDO. None quando a pergunta nao cabe na fonte.

    UMA regra para TODOS os construtores (pattern, count, metricas, rollup). Antes so o
    pattern-path resolvia: os outros liam `parquet_source` cru e mandavam `{mes_arquivo}`
    literal para o DuckDB — 404 na cara do usuario. Fonte sem placeholder
    passa intacta.
    """
    src = str(source or "")
    if FONTE_POR_DIA in src:
        dia = dia_unico_da_pergunta(params)
        # fonte de UM dia nao cobre um periodo: recusar e melhor que responder 1 dia calado
        return src.replace(FONTE_POR_DIA, dia) if dia else None
    if FONTE_POR_MES in src:
        return _fonte_dos_meses(src, params)
    return src


def _predicado_de_placeholder(template: str, chave: str):
    """(match, coluna) do predicado `col = '{chave}'` / `col IN ('{chave}')` no template."""
    padrao = re.compile(
        r"(?:\bAND\b\s*|\bWHERE\b\s*)?([a-z_][a-z0-9_.]*)\s*(?:=|\bIN\b)\s*\(?\s*'?\{"
        + re.escape(chave) + r"\}'?\s*\)?\s*",
        re.IGNORECASE,
    )
    m = padrao.search(template)
    return (m, m.group(1)) if m else (None, None)


def _sem_filtro_de_dimensao(template: str, params: dict, validated: dict):
    """Remove do template os filtros de DIMENSAO que o usuario NAO pediu.

    O placeholder de dimensao sem valor na pergunta seria preenchido com o validated_params
    (a AMOSTRA usada na validacao do pattern) e a resposta sairia restrita a uma entidade
    arbitraria, sem aviso. Regra:
      - coluna no GROUP BY  -> tira o predicado (resposta com TODAS as entidades, com quebra);
      - coluna fora do GROUP BY -> pattern RECUSADO (None): agregar entidades caladamente e pior.
    Devolve o template (possivelmente sem o predicado) ou None se o pattern for inviavel.
    """
    chaves = [k for k in re.findall(r"\{([a-z_]+)\}", template)
              if k not in params and k in validated
              and k not in _TEMPORAL_PARAM_KEYS and not _TEMPORAL_KEY_NAME_RE.search(k)]
    if not chaves:
        return template
    # clausula GROUP BY isolada (ate ORDER BY/HAVING/LIMIT ou fim) + lista do SELECT
    mg = re.search(r"\bGROUP\s+BY\b(.*?)(?=\bORDER\s+BY\b|\bHAVING\b|\bLIMIT\b|$)",
                   template, re.IGNORECASE | re.DOTALL)
    grupo_txt = (mg.group(1) if mg else "").upper()
    posicional = bool(grupo_txt.strip()) and not re.search(r"[A-Z_]", grupo_txt)
    select_txt = re.split(r"\bFROM\b", template, maxsplit=1, flags=re.IGNORECASE)[0].upper()
    for chave in chaves:
        m, coluna = _predicado_de_placeholder(template, chave)
        if m is None or not coluna:
            return None  # placeholder de dimensao fora de predicado simples: nao mexer
        col_up = coluna.split(".")[-1].upper()
        # a coluna precisa sobreviver na SAIDA: nomeada no GROUP BY, ou no SELECT quando o
        # GROUP BY e posicional (GROUP BY 1, 2)
        no_group = col_up in grupo_txt or (posicional and col_up in select_txt)
        if not no_group:
            return None
        inicio, fim = m.span()
        trecho = m.group(0)
        # se o predicado carregava o WHERE, a proxima clausula precisa virar WHERE
        if re.match(r"\s*WHERE\b", trecho, re.IGNORECASE):
            resto = template[fim:]
            resto = re.sub(r"^\s*AND\b", " WHERE", resto, count=1, flags=re.IGNORECASE)
            template = template[:inicio] + resto
        else:
            template = template[:inicio] + " " + template[fim:]
    return template


def _fill_params(
    template: str,
    source: str,
    params: dict,
    validated_params: Optional[dict] = None,
) -> Optional[str]:
    """Preenche placeholders de um template. Retorna None se faltar placeholder essencial.

    Cadeia de resolucao POR CHAVE: params (explicito) > validated_params (provado
    na validacao por execucao) > default estatico do engine.
    """
    validated = validated_params if isinstance(validated_params, dict) else {}

    # Janela MULTI-ANO: so um template com placeholder de INTERVALO consegue
    # expressar a janela. Template de ano unico ({year}) preencheria so o
    # primeiro ano (ex.: 2025-12-29 a 2026-01-04 viraria EXTRACT(YEAR)=2025) e
    # template SEM filtro temporal leria a serie inteira — ambos silenciosamente
    # errados. Pattern inviavel: cai para um pattern de range ou para o
    # build_from_metrics (que aplica a janela exata de datas).
    _ys, _ye = params.get("year_start"), params.get("year_end")
    if _ys is not None and _ye is not None and _ys != _ye:
        tem_range = re.search(
            r"\{(?:year_start|year_end|ano_inicio|ano_fim|start_date|end_date|data_inicio|data_fim|start|end)\}",
            template,
        )
        if not tem_range:
            return None

    def _valor_param(chave: str, default):
        valor = params.get(chave)
        if valor is not None:
            return valor
        # Chaves TEMPORAIS nunca resolvem de validated_params: sao artefatos da
        # validacao-por-execucao (datas de amostra, ex. primeira data da serie)
        # e responderiam a pergunta com o periodo errado. Dimensoes (subsistema,
        # estado etc.) continuam valendo do contrato.
        if chave not in _TEMPORAL_PARAM_KEYS:
            valor = validated.get(chave)
            if valor is not None:
                return valor
        return default

    source = resolver_fonte(source, params)
    if source is None:
        return None

    year = _valor_param("year", datetime.now().year)
    year_start = _valor_param("year_start", year)
    year_end = _valor_param("year_end", year)
    data_val = _valor_param("data", f"{year}-01-01")
    mapping = {
        "source": source,
        "year": year,
        "year_filter": _valor_param("year_filter", year),
        "year_start": year_start,
        "year_end": year_end,
        "month": _valor_param("month", 1),
        "n": _valor_param("n", 10),
        "top_n": _valor_param("top_n", 10),
        "data_inicio": _valor_param("data_inicio", f"{year}-01-01"),
        "data_fim": _valor_param("data_fim", f"{year}-12-31"),
        "start_date": _valor_param("start_date", f"{year}-01-01"),
        "end_date": _valor_param("end_date", f"{year}-12-31"),
        "start": _valor_param("start", f"{year}-01-01"),
        "end": _valor_param("end", f"{year}-12-31"),
        "data": data_val,
        # Fonte com UM ARQUIVO POR DIA (INPE focos diario): a data vive no NOME do arquivo.
        # Congelar essa data no contrato faz o dataset responder o dia errado com cara de
        # certo. O contrato declara {data_arquivo}; aqui vira YYYYMMDD do dia
        # PEDIDO — sem data na pergunta, ONTEM (a publicacao do INPE e D-1).
        "data_arquivo": dia_unico_da_pergunta(params) or "",
        # Aliases temporais usados por patterns do corpus ({date}, {data_ref},
        # {ano_inicio}/{ano_fim}). Sem mapeamento cairiam no fallback generico,
        # que resolvia de validated_params (datas de amostra stale da validacao).
        "date": _valor_param("date", data_val),
        "data_ref": _valor_param("data_ref", data_val),
        "ano_inicio": _valor_param("ano_inicio", year_start),
        "ano_fim": _valor_param("ano_fim", year_end),
    }

    # Datas em placeholders NUS (sem aspas) -> literal DATE/TIMESTAMP, evitando
    # aritmetica inteira / BinderException no DuckDB (ex.: `BETWEEN {start} AND {end}`).
    # Valor com componente de hora (ex. fim-de-dia '... 23:59:59') vira TIMESTAMP —
    # literal DATE nao aceita hora. Placeholders ja entre aspas ('{x}') ficam para o
    # mapping normal (-> string 'YYYY-MM-DD[ HH:MM:SS]').
    _date_keys = ("data_inicio", "data_fim", "start_date", "end_date", "start", "end", "data", "date", "data_ref")

    def _date_literal(m: re.Match) -> str:
        valor = str(mapping[m.group(1)])
        tipo = "TIMESTAMP" if " " in valor else "DATE"
        return f"{tipo} '{valor}'"

    template = re.sub(
        r"(?<!')\{(" + "|".join(_date_keys) + r")\}(?!')",
        _date_literal,
        template,
    )

    def repl(match: re.Match) -> str:
        key = match.group(1)
        if key in mapping:
            return str(mapping[key])
        # Tenta params explicito (chaves extras do caller)
        valor = params.get(key)
        if valor is not None:
            return str(valor)
        # Contratos validados podem provar placeholders especificos do pattern
        # que nao fazem parte do mapping generico do engine — MAS nunca chaves
        # de cara temporal (mesma regra do mapping: validated temporal e data de
        # amostra stale; melhor deixar o pattern inviavel e cair no fallback).
        if key in validated and not _TEMPORAL_KEY_NAME_RE.search(key):
            return str(validated[key])
        return match.group(0)  # mantem desconhecido para deteccao abaixo

    # DIMENSAO NAO PEDIDA nao vira filtro: "carga no dia X" respondia so o
    # subsistema 'N' porque o placeholder caia no validated_params — que e a AMOSTRA da
    # validacao, nao a intencao do usuario. Ver _sem_filtro_de_dimensao.
    template = _sem_filtro_de_dimensao(template, params, validated)
    if template is None:
        return None

    filled = re.sub(r"\{([a-z_]+)\}", repl, template)
    # 2o passe: o valor de {source} pode ele mesmo trazer placeholder (fonte com um arquivo
    # por dia). re.sub nao re-varre o texto inserido, entao o {data_arquivo} vindo da fonte
    # so resolve aqui. Um passe extra basta e termina (a fonte nao se auto-referencia).
    if "{" in filled:
        filled = re.sub(r"\{([a-z_]+)\}", repl, filled)

    # Placeholders nao resolvidos (ex.: {reservatorio}, {tipo_usina}) -> pattern inviavel
    leftover = re.findall(r"\{[a-z_]+\}", filled)
    if leftover:
        return None
    return filled


# ── Detector de recencia (A1) ─────────────────────────────────────────────────

_RECENCIA_TERMS = ("mais recente", "atual", "vigente", "ultimo", "ultima", "hoje", "ontem")


def _pede_recencia(p_norm: str) -> bool:
    """True se a pergunta pede o dado mais recente/atual/vigente/ultimo."""
    return any(t in p_norm for t in _RECENCIA_TERMS)


# Placeholders temporais nos templates de sql_patterns (A1)
_TEMPORAL_PLACEHOLDERS = frozenset({"mes", "mes_referencia", "competencia", "month", "year", "ano", "year_filter"})

# Subconjuntos: qual dado na pergunta cobre qual placeholder
_YEAR_PLACEHOLDERS = frozenset({"year", "year_filter", "ano"})
_MONTH_PLACEHOLDERS = frozenset({"mes", "mes_referencia", "competencia", "month"})


def _template_has_temporal_placeholder(template: str) -> bool:
    """Verifica se o template usa algum placeholder temporal."""
    for ph in _TEMPORAL_PLACEHOLDERS:
        if "{" + ph + "}" in template:
            return True
    return False


def _score_pattern(name: str, body: dict, pergunta: str, mode: str, params: dict | None = None) -> int:
    """Pontua aderencia de um sql_pattern a pergunta.

    Incorpora:
    - A1 (recencia): bonus p/ patterns 'ultimo/recente' ou ORDER BY DESC LIMIT 1
      quando a pergunta pede recencia; penalidade p/ patterns com placeholder temporal
      que seria preenchido por validated_params/default.
    - Sinais existentes: tokens do nome, output_modes, grain.
    """
    p_norm = _norm(pergunta)
    name_norm = _norm(name)
    score = 0
    for tok in re.findall(r"[a-z]+", name_norm):
        if len(tok) > 3 and tok in p_norm:
            score += 2
    matches = body.get("matches") or {}
    out_modes = matches.get("output_modes") or []
    if mode and mode in out_modes:
        score += 5
    grain = _norm(str(matches.get("grain", "")))
    grain_words = {"month": "mensal", "day": "diari", "year": "anual", "hour": "horari"}
    if grain in grain_words and grain_words[grain] in p_norm:
        score += 2
    # DIA ESPECIFICO na pergunta ("no dia 01/09/2026"): o grao do pattern tem que ser o dia
    # (ou mais fino). Sem isto o pattern mensal com {year} respondia o ANO inteiro.
    dia = (params or {}).get("data")
    if dia and str((params or {}).get("data_inicio", dia)).startswith(str(dia)):
        if grain in ("day", "hour"):
            score += 6
        elif grain in ("month", "year"):
            score -= 6

    # ── A1: bonus/penalidade de recencia ──────────────────────────────────
    template = body.get("template") or ""
    if _pede_recencia(p_norm):
        has_temporal_ph = _template_has_temporal_placeholder(template)
        # Bonus: pattern cujo nome indica recencia OU template com ORDER BY...DESC...LIMIT 1
        recencia_nome = any(t in name_norm for t in ("ultimo", "recente", "vigente", "atual"))
        template_norm = _norm(template)
        # LIMIT 1 exato (\b): "limit 10" NAO e pattern de registro unico
        recencia_template = bool(re.search(r"order\s+by\b.*\bdesc\b.*\blimit\s+1\b", template_norm, re.S))
        if (not has_temporal_ph) and (recencia_nome or recencia_template):
            score += 8
        # Penalidade: pattern com placeholder temporal que so resolveria por
        # validated_params OU default (pergunta de recencia sem ano/mes literal).
        # Penaliza POR PLACEHOLDER presente no template; cumulativo. NAO depende
        # de validated_params existir (senao a penalidade fica inoperante num
        # corpus sem params provados).
        if has_temporal_ph:
            pergunta_tem_ano = bool(re.search(r"\b(19\d{2}|20\d{2})\b", pergunta))
            pergunta_tem_mes = any(
                re.search(rf"\b{re.escape(m)}\b", p_norm)
                for m in (
                    "janeiro",
                    "fevereiro",
                    "marco",
                    "abril",
                    "maio",
                    "junho",
                    "julho",
                    "agosto",
                    "setembro",
                    "outubro",
                    "novembro",
                    "dezembro",
                )
            )
            tpl = template
            for ph in _TEMPORAL_PLACEHOLDERS:
                if ("{" + ph + "}") not in tpl:
                    continue
                if ph in _YEAR_PLACEHOLDERS and pergunta_tem_ano:
                    continue
                if ph in _MONTH_PLACEHOLDERS and pergunta_tem_mes:
                    continue
                score -= 6

    return score


# ── A2: Entidade da pergunta vence default validated_params ───────────────────


def _resolve_placeholder_from_question(placeholder: str, pergunta_norm: str, meta: dict) -> Optional[str]:
    """Tenta resolver um placeholder de dimensao casando a pergunta contra enum/aliases.

    Retorna o valor CANONICO (ex.: 'SUDESTE') ou None se nao encontrou match.
    Match deterministico: normalizado sem acento, frase > palavra,
    tokens com >=4 chars; empate -> valor mais longo.
    """
    columns = meta.get("columns") or []
    semantics = meta.get("semantics") or {}
    value_aliases = semantics.get("value_aliases") or {}
    ph_norm = _norm(placeholder)

    # 1. Procura coluna cujo nome normalizado corresponde ao placeholder
    target_col = None
    for col in columns:
        col_name_norm = _norm(col.get("name", ""))
        if ph_norm in col_name_norm or col_name_norm.endswith(ph_norm):
            target_col = col
            break

    # 2. Candidatos: enum da coluna + examples + value_aliases com a mesma chave
    candidates: list[tuple[str, str]] = []  # (alias_norm, valor_canonico)

    if target_col:
        for val in target_col.get("enum") or []:
            val_str = str(val)
            candidates.append((_norm(val_str), val_str))
        for val in target_col.get("examples") or []:
            if isinstance(val, str):
                candidates.append((_norm(val), val))

    # Dos value_aliases com chave == placeholder (ou contendo placeholder)
    for alias_key, members in value_aliases.items():
        if not isinstance(members, dict):
            continue
        ak = _norm(alias_key)
        if ph_norm not in ak and ak not in ph_norm:
            continue
        for canon, info in members.items():
            if not isinstance(info, dict):
                continue
            candidates.append((_norm(str(canon)), str(canon)))
            for alias in info.get("aliases") or []:
                candidates.append((_norm(str(alias)), str(canon)))

    if not candidates:
        return None

    # 3. Match: deterministico, frase > palavra, tokens >=4 chars
    matches_found: list[tuple[str, int]] = []
    for alias_norm, canon in candidates:
        if not alias_norm or len(alias_norm) < 3:
            continue
        if " " in alias_norm:
            if alias_norm in pergunta_norm:
                matches_found.append((canon, len(alias_norm) + 100))
        elif len(alias_norm) >= 4:
            stem_a = _stem(alias_norm)
            for tok in re.findall(r"\b\w+\b", pergunta_norm):
                if len(tok) >= 4 and _stem(tok) == stem_a:
                    matches_found.append((canon, len(alias_norm)))
                    break

    if not matches_found:
        return None

    # Empate -> valor mais longo (mais especifico)
    matches_found.sort(key=lambda x: x[1], reverse=True)
    return matches_found[0][0]


