"""buscar_dataset — busca no catalogo de contratos (este corpus: ONS).

Cada candidato traz RELEVANCIA em faixa (alta/media/fraca) com os TERMOS da pergunta
que casaram. Quando o melhor candidato e fraco, o cabecalho diz "Nenhum dataset
relevante" antes de listar os candidatos fracos.
"""
import re
import unicodedata

from mcp_tiago_dados_abertos.catalogo.contracts import rank_datasets
from mcp_tiago_dados_abertos.catalogo.ranking import PERIODO_WORDS, STOPWORDS_CONVERSA
from mcp_tiago_dados_abertos.tools.descrever_dataset import fonte_legivel

_STOP = {"de", "do", "da", "dos", "das", "em", "no", "na", "nos", "nas", "por", "para", "com", "o", "a", "os",
         "as", "e", "ou", "que", "qual", "quais", "como", "foi", "ser", "ter", "ano", "mes", "dia"}
# Termos que a pergunta traz e que NENHUM contrato casa por natureza: numero (ano, valor),
# marca de periodo ("ultimos", "mensal"), nome de mes e palavra de conversa. Contando esses
# termos, o cabecalho "Nenhum dataset relevante" saia em 95% das perguntas que TINHAM
# resposta — inclusive com o dataset certo em primeiro.
_MESES = {"janeiro", "fevereiro", "marco", "abril", "maio", "junho", "julho", "agosto", "setembro",
          "outubro", "novembro", "dezembro", "jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago",
          "set", "out", "nov", "dez"}


def _norm(s: str) -> str:
    return unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode("ascii").lower()


def termos_da_pergunta(tema: str) -> set[str]:
    """Termos da pergunta que um contrato PODE casar: sem numero, periodo, mes ou conversa."""
    toks = {t for t in re.findall(r"\b\w+\b", _norm(tema)) if len(t) >= 3 and t not in _STOP}
    return {t for t in toks
            if not t.isdigit() and t not in PERIODO_WORDS and t not in _MESES and t not in STOPWORDS_CONVERSA}


def _texto_forte(meta: dict) -> str:
    """Campos em que o dataset se DECLARA sobre o assunto: nome, tags, vector_tags, perguntas
    tipicas e aliases de coluna. Casar aqui e evidencia de que o tema e o do dataset."""
    campos = [str(meta.get("name") or "").replace("-", " ").replace("_", " ")]
    campos += [str(t) for t in (meta.get("tags") or []) + (meta.get("vector_tags") or [])]
    campos += [str(q) for q in (meta.get("typical_questions") or [])]
    for c in meta.get("columns") or []:
        if isinstance(c, dict):
            campos += [str(a) for a in (c.get("aliases") or [])]
    return _norm(" ".join(campos))


def _casa(termo: str, texto: str) -> bool:
    return termo in texto or (len(termo) > 3 and termo.rstrip("s") in texto)


def termos_casados(termos: set[str], meta: dict) -> set[str]:
    """Termos da pergunta presentes em qualquer campo do contrato (forte ou prosa)."""
    texto = _texto_forte(meta) + " " + _norm(str(meta.get("rag_context") or ""))
    return {t for t in termos if _casa(t, texto)}


def faixa_relevancia(termos: set[str], meta: dict) -> str:
    """Faixa pela EVIDENCIA, nao pela escala interna do score:
      alta  — TODOS os termos casam em campo FORTE (o dataset se declara sobre isso);
      media — pelo menos METADE dos termos casa, em campo forte ou na prosa do rag_context;
      fraca — menos da metade dos termos da pergunta casa em lugar nenhum.
    Sem ratio sobre o score: o agente ve por que o candidato esta ali. Exigir TODO termo
    marcava "fraca" em 85% das perguntas que TINHAM resposta — nome de lugar, de empresa
    ou de usina nunca casa contrato — contra 94% das que nao tinham; com a metade, 37%
    contra 75%. E' a regra que separa os dois."""
    if not termos:
        return "fraca"
    forte = _texto_forte(meta)
    prosa = _norm(str(meta.get("rag_context") or ""))
    em_forte = {t for t in termos if _casa(t, forte)}
    em_prosa = {t for t in termos if _casa(t, prosa)}
    casados = em_forte | em_prosa
    if not casados or len(casados) * 2 < len(termos):
        return "fraca"
    return "alta" if em_forte >= termos else "media"


# Quantos candidatos a busca devolve. O dataset certo esta no top-3 em 85%, no top-5 em
# 90% e no top-10 em 96%. Dez candidatos com 'Quando usar' custam cerca de 3x o texto de
# tres e ainda menos que a lista completa (listar_datasets); o modelo cliente escolhe.
TOP_N = 10


async def buscar_dataset(tema: str, *, catalog: dict, orgao: str | None = None) -> str:
    """Descobre qual dataset responde a um tema, em qualquer portal (ou so no `orgao`)."""
    if not catalog:
        return "[TIAGO Dados Abertos] Catalogo vazio."
    orgao_f = (orgao or "").strip().lower() or None
    ranked = rank_datasets(catalog, tema, top_n=TOP_N, orgao=orgao_f)
    if not ranked:
        return (f"Nenhum dataset para '{tema}'" + (f" no portal {orgao_f.upper()}." if orgao_f else ".")
                + " Chame listar_datasets para escolher pelo catalogo inteiro.")
    termos = termos_da_pergunta(tema)
    linhas = []
    melhor = None
    for rank_pos, (score, name, meta) in enumerate(ranked, 1):
        casados = termos_casados(termos, meta)
        faixa = faixa_relevancia(termos, meta)
        melhor = melhor or faixa
        orgao_tag = str(meta.get("orgao", "?")).upper()
        ev = ", ".join(sorted(casados)) if casados else "nenhum termo da pergunta"
        linhas.append(f"### {rank_pos}. [{orgao_tag}] `{name}` — relevancia: {faixa} "
                      f"(score {score}; termos casados: {ev})")
        linhas.append(f"**parquet_source:** `{fonte_legivel(meta.get('parquet_source', 'N/A'))}`")
        if meta.get("granularity"):
            linhas.append(f"**Granularidade:** {meta['granularity']}")
        if meta.get("rag_context"):
            linhas.append(f"**Quando usar:** {meta['rag_context'][:200]}")
        # Pergunta de exemplo, NAO o SQL: mostra que tipo de pergunta o dataset responde,
        # que e o sinal util para escolher entre candidatos parecidos. O SQL correspondente
        # so aparece no descrever_dataset, onde ha espaco para ele.
        fqs = meta.get("few_shot_queries", [])
        if isinstance(fqs, list) and fqs and isinstance(fqs[0], dict):
            linhas.append(f"**Pergunta de exemplo:** {fqs[0].get('question', '')[:100]}")
        linhas.append("")
    # DESCOBERTA POR ORGAO: "consumo" so trazia CCEE e o agente concluia que
    # o ONS nao tinha o dado — a carga do ONS ficava em 4o/5o. O ranking NAO muda; depois dos
    # TOP_N vem o MELHOR candidato de cada OUTRO orgao, para o agente saber que existe.
    if not orgao_f:
        ja = {str((m or {}).get("orgao", "")).lower() for _, _, m in ranked}
        outros = []
        for score, name, meta in rank_datasets(catalog, tema, top_n=40):
            org = str((meta or {}).get("orgao", "")).lower()
            if score <= 0 or not org or org in ja:
                continue
            ja.add(org)
            outros.append(f"- [{org.upper()}] `{name}` — relevancia: {faixa_relevancia(termos, meta)} "
                          f"(score {score}) — `{meta.get('parquet_source', 'N/A')}`")
        if outros:
            linhas.append("### Outros orgaos (melhor de cada um)")
            linhas.extend(outros)
            linhas.append("")

    if melhor == "fraca":
        cab = (f"## Nenhum dataset relevante para: '{tema}'\n\n"
               "Os candidatos abaixo sao FRACOS (poucos termos da pergunta casaram). Reformule com termos do "
               "dominio (ex.: nome da grandeza, portal, subsistema), ou chame listar_datasets e escolha pelo "
               "catalogo inteiro; confirme com descrever_dataset antes de usar.\n")
    else:
        cab = f"## Datasets para: '{tema}'\n"
    return "\n".join([cab] + linhas)
