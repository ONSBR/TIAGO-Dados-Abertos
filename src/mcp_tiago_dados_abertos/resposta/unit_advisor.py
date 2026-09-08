# -*- coding: utf-8 -*-
"""Advisory de unidade em SQL de usuario/LLM — CONTRACT-DRIVEN, sem tabela de fatores.

O passo de cada serie esta DECLARADO no contrato (metrics[].interval_hours);
este advisor compara o fator literal de conversao de energia no SQL com essa
declaracao e ANEXA um aviso quando divergem (nunca bloqueia). Contrato sem
interval_hours -> silencio (omission-safe).
"""

import re

_FATOR_RE = re.compile(r"\*\s*(\d+(?:\.\d+)?)\s*\)?\s*/\s*1000(?:\.0)?\b")
_PREFIXO_RE = re.compile(r"s3://[^'\"\s]+/dataset/([^/'\"]+)/")


def aviso_unidade(sql: str, catalog: dict) -> str:
    """Retorna aviso (ou "") quando o fator de conversao de energia no SQL
    diverge do passo declarado no contrato do dataset referenciado."""
    fm = _FATOR_RE.search(sql or "")
    if not fm:
        return ""
    fator = float(fm.group(1))

    pm = _PREFIXO_RE.search(sql)
    if not pm:
        return ""
    prefixo = pm.group(1)

    meta = None
    for m in catalog.values():
        fontes = f"{m.get('parquet_source') or ''} {m.get('s3_location') or ''}"
        if f"/dataset/{prefixo}/" in fontes:
            meta = m
            break
    if not meta:
        return ""

    ihs = set()
    for mt in (meta.get("semantics") or {}).get("metrics") or []:
        ih = mt.get("interval_hours")
        if isinstance(ih, (int, float)) and ih > 0:
            ihs.add(float(ih))
    if not ihs or any(abs(fator - ih) < 1e-9 for ih in ihs):
        return ""

    decl = "/".join(f"{ih:g}" for ih in sorted(ihs))
    nome = meta.get("name", prefixo)
    return (
        f"\n\n*[AVISO UNIDADE] o SQL multiplica por {fator:g} na conversao de energia, "
        f"mas o contrato de `{nome}` declara passo de {decl}h por linha (interval_hours). "
        f"Total possivelmente errado por esse fator — confira a granularity via "
        f"descrever_dataset('{nome}') antes de reportar.]*"
    )
