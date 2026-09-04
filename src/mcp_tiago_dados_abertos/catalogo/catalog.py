"""Catalogo ODCS — carrega contratos ONS."""

import hashlib
import json
import logging
from pathlib import Path
from typing import Optional

from mcp_tiago_dados_abertos.catalogo.ranking import (
    rank_datasets,  # noqa: F401  (porta historica: catalog.rank_datasets)
)
from mcp_tiago_dados_abertos.infra.config import CONTRACTS_DIR

logger = logging.getLogger("mcp_tiago_dados_abertos.catalog")


def _portal_url(data: dict, custom: dict) -> str:
    """URL canonica do portal, do bloco ODCS authoritativeDefinitions.

    E o slug REAL do portal (pode divergir do nome do contrato — ex.:
    taxa_teif_teip vs taxa-teifa-teip); nunca montar por concatenacao.

    A regra era o literal "dados.ons.org.br/dataset/", o que zerava a URL de todo
    contrato de outro orgao — e `provenance` OMITE fonte sem portal_url, entao a
    resposta saia sem link. O criterio agora e o do proprio ODCS: a definicao
    autoritativa de tipo businessDefinition. `customProperties.portalUrl` e o
    fallback (a convencao que os corpora gerados fora do ONS usam).
    """
    for ad in data.get("authoritativeDefinitions", []) or []:
        if not isinstance(ad, dict):
            continue
        url = str(ad.get("url", "")).strip()
        if ad.get("type") == "businessDefinition" and url.startswith(("http://", "https://")):
            return url
    fallback = str(custom.get("portalUrl", "") or "").strip()
    if fallback.startswith(("http://", "https://")):
        return fallback
    return ""  # sem definicao autoritativa => sem URL (nunca montar/chutar)


def _cache_valido(path: Path) -> bool:
    """Valida contracts_cache.json: version, docs nao-vazio, n_docs coerente, hash ok.

    Resolve F2 (cache sem validacao): um cache PARCIAL/truncado seria servido em
    silencio. Agora validamos antes de confiar.

    Retorna True somente se:
    - JSON valido
    - tem "version"
    - tem "docs" nao-vazio
    - se "n_docs" presente: n_docs == len(docs)
    - se "content_hash" presente: recomputa e exige igualdade
    - se "yaml_manifest" presente: os YAML em disco tem que ser os do build (nome, tamanho, mtime)

    Qualquer excecao/parse-erro -> False (nunca propaga).
    Cache legado (sem n_docs/hash) continua aceito (retrocompat).
    """
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        if "version" not in raw:
            return False
        docs = raw.get("docs")
        if not docs or not isinstance(docs, list):
            return False
        # Validacao de n_docs (se presente)
        if "n_docs" in raw:
            if raw["n_docs"] != len(docs):
                return False
        # Manifesto dos YAML (se presente): YAML editado/adicionado depois do build invalida
        if "yaml_manifest" in raw:
            base = Path(path).parent
            itens = sorted((str(p.relative_to(base)).replace(chr(92), '/'), p.stat().st_size, p.stat().st_mtime_ns)
                           for p in base.rglob('*.odcs.yaml'))
            if raw["yaml_manifest"] != hashlib.sha256(json.dumps(itens).encode("utf-8")).hexdigest():
                return False
        # Validacao de content_hash (se presente)
        if "content_hash" in raw:
            content = json.dumps(docs, sort_keys=True, ensure_ascii=False)
            computed = hashlib.sha256(content.encode("utf-8")).hexdigest()
            if raw["content_hash"] != computed:
                return False
        return True
    except Exception:
        return False


def load_all_contracts() -> dict:
    """Carrega contratos ODCS do ONS.
    Retorna dict: orgao/nome_dataset -> meta
    """
    try:
        import yaml
    except ImportError:
        logger.warning("PyYAML nao instalado.")
        return {}

    # libyaml (CSafeLoader) e 10-30x mais rapido que o SafeLoader puro-python.
    # CRITICO em prod: cada instancia parseia TODOS os contratos no warmup e as
    # requests BLOQUEIAM nesse parse (~20s de CPU por chamada, provado por
    # sampler 12/08/2026). yaml.safe_load NUNCA usa o C loader — tem que pedir.
    loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)

    def _yaml_load(text: str):
        return yaml.load(text, Loader=loader)

    catalog = {}
    if not CONTRACTS_DIR.exists():
        logger.warning("Pasta de contratos nao encontrada: %s", CONTRACTS_DIR)
        return catalog

    # Cache pre-parseado (scripts/build_contracts_cache.py): 1 json.loads em vez
    # de um parse YAML por contrato — corta o warmup de ~60s p/ ~1s. Fallback
    # integral para YAML se ausente/corrompido (publicacao antiga ainda funciona).
    docs: list[tuple[str, str, dict]] = []
    _cache = CONTRACTS_DIR / "contracts_cache.json"
    if _cache.exists():
        try:
            import json as _json

            def _date_hook(obj: dict):
                if "__date__" in obj and len(obj) == 1:
                    import datetime as _dt

                    return _dt.date.fromisoformat(obj["__date__"])
                if "__datetime__" in obj and len(obj) == 1:
                    import datetime as _dt

                    return _dt.datetime.fromisoformat(obj["__datetime__"])
                return obj

            # F2 (rede de seguranca): validar manifesto antes de confiar no cache.
            # Delega a _cache_valido que parseia SEM hook (sem risco de TypeError
            # com datas) e valida version+n_docs+hash. Mesmo criterio do resolver.
            if not _cache_valido(_cache):
                logger.warning("contracts_cache.json invalido/parcial — caindo p/ YAML")
                docs = []
            else:
                _raw = _json.loads(_cache.read_text(encoding="utf-8"), object_hook=_date_hook)
                docs = [(d["orgao"], d["stem"], d["data"]) for d in _raw["docs"]]
                logger.info("catalogo via contracts_cache.json (%d docs)", len(docs))

        except Exception as _e:
            logger.warning("contracts_cache.json invalido (%s) — caindo p/ YAML", _e)
            docs = []
    if not docs:
        for orgao_dir in CONTRACTS_DIR.iterdir():
            if not orgao_dir.is_dir() or orgao_dir.name.startswith("."):
                continue
            for f in orgao_dir.glob("*.odcs.yaml"):
                try:
                    _data = _yaml_load(f.read_text(encoding="utf-8"))
                except Exception as _e:
                    logger.warning("Falha ao parsear %s: %s", f.name, _e)
                    continue
                if _data:
                    docs.append((orgao_dir.name, f.stem, _data))

    for orgao, _stem, data in docs:
        if True:
            class _F:  # compat: mantem f.name no except historico abaixo
                name = _stem

            f = _F()
            try:
                if not data:
                    continue
                name = data.get("name", _stem.replace(".odcs", ""))
                key = f"{orgao}/{name}"
                custom = {cp.get("property", ""): cp.get("value", "") for cp in data.get("customProperties", [])}
                # freshness vive em slaProperties, nao em customProperties
                sla_props = {sp.get("property", ""): sp.get("value", "") for sp in data.get("slaProperties", [])}
                columns = []
                # spatial_aggregate: detectado quando uma coluna declara aggregateMember
                _spatial_aggregate = None
                for schema_obj in data.get("schema", []):
                    for prop in schema_obj.get("properties", []):
                        col = {
                            "name": prop.get("name"),
                            "type": prop.get("physicalType"),
                            # logicalType preservado: o aviso de TRY_CAST no
                            # render depende dele (VARCHAR fisico + number
                            # logico = metrica em texto)
                            "logical": prop.get("logicalType", ""),
                            "description": prop.get("description", ""),
                            "unit": prop.get("unit", ""),
                            # examples: valores REAIS do dado. Sao evidencia de
                            # roteamento (a pergunta citar um valor do dataset vale
                            # +10 no rank) e candidatos na resolucao de filtro. Sem
                            # esta linha, os dois caminhos liam sempre vazio.
                            "examples": prop.get("examples") or [],
                        }
                        col_enum = None
                        agg_member = None
                        if prop.get("enum"):
                            col["enum"] = prop["enum"]
                            col_enum = prop["enum"]
                        for cp in prop.get("customProperties", []):
                            if cp.get("property") == "castExpression":
                                col["cast"] = cp.get("value")
                            elif cp.get("property") == "aliases":
                                col["aliases"] = cp.get("value", [])
                            elif cp.get("property") == "semanticType":
                                col["semantic_type"] = cp.get("value", "")
                            elif cp.get("property") == "enum":
                                col["enum"] = cp.get("value", [])
                                col_enum = cp.get("value", [])
                            elif cp.get("property") == "aggregateMember":
                                agg_member = cp.get("value")
                        # Se a coluna declara aggregateMember, monta spatial_aggregate
                        if agg_member and col_enum:
                            base_members = [m for m in col_enum if m != agg_member]
                            _spatial_aggregate = {
                                "column": prop.get("name"),
                                "total_member": agg_member,
                                "base_members": base_members,
                            }
                        columns.append(col)
                s3 = next(
                    (s.get("location", "") for s in data.get("servers", []) if s.get("type") == "s3"),
                    "",
                )
                # Alguns contratos embrulham o bloco em customProperties.semantics.semantics
                # (envelope aninhado). Desembrulha para o nivel real de metrics/sql_patterns.
                _semantics = custom.get("semantics") or {}
                if isinstance(_semantics, dict) and isinstance(_semantics.get("semantics"), dict):
                    _semantics = _semantics["semantics"]
                catalog[key] = {
                    "orgao": orgao,
                    "name": name,
                    "s3_location": s3,
                    # O corpus nomeia o campo 'sourceExpression'; ha' corpora que usam 'parquetSource'.
                    # Le ambos (read_parquet(...) embrulhado); fallback = location s3 crua.
                    "parquet_source": custom.get("parquetSource") or custom.get("sourceExpression") or s3,
                    # Poda de arquivos por ano (partition pruning): a flag vive NO CONTRATO
                    # (escrita por validate_pruning.py -> patch_prune_flag.py). "" = nao podar.
                    "prune_partition": custom.get("partitionPruning", ""),
                    "portal_url": _portal_url(data, custom),
                    # Duas convencoes vivem no mesmo corpus: contratos migrados usam os nomes em
                    # ingles. Ler so' um lado deixa o campo VAZIO — e vazio nao levanta
                    # erro, some calado (mesmo padrao de `parquetSource`/`sourceExpression`).
                    "rag_context": custom.get("ragContext") or custom.get("aiContext", ""),
                    "agent_instructions": custom.get("agentInstructions", ""),
                    "typical_questions": custom.get("typicalQuestions", []),
                    "few_shot_queries": custom.get("fewShotQueries", []),
                    "granularity": custom.get("granularity", ""),
                    "spatial_granularity": custom.get("spatialGranularity", ""),
                    "period_start": custom.get("periodoCoberturaInicio") or custom.get("temporalCoverageStart", ""),
                    "period_end": custom.get("periodoCoberturaFim") or custom.get("temporalCoverageEnd", ""),
                    # 'freshness' e a convencao do corpus ONS; 'frequency' a dos
                    # demais portais. Mesma coisa (duracao ISO-8601), mesma vitrine.
                    "freshness": sla_props.get("freshness") or sla_props.get("frequency", ""),
                    "sla": custom.get("slaAtualizacao", ""),
                    "primary_key": custom.get("primaryKey", []),
                    "related_datasets": custom.get("relatedDatasets", {}),
                    "tags": data.get("tags", []),
                    "vector_tags": custom.get("vectorTags", []),
                    "columns": columns,
                    "status": data.get("status", "active"),
                    # Bloco semantico avancado (query_routing, sql_patterns,
                    # value_aliases, aggregation_modes, ambiguity_policy, safe_joins).
                    # Fonte de verdade para o motor deterministico (semantics_engine).
                    "semantics": _semantics,
                    # Agregado espacial (SIN rollup): quando uma coluna declara aggregateMember,
                    # indica que o dataset tem uma linha agregada (ex. SIN) e membros base (N,NE,S,SE).
                    # Permite rollup deterministico evitando o bug "÷4".
                    "spatial_aggregate": _spatial_aggregate,
                }
            except Exception as e:
                logger.warning("Erro ao carregar %s: %s", f.name, e)

    # Overlay de casts MEDIDOS (_measured_casts.json): camada humana que corrige
    # o decimal pt-BR sem reescrever contrato. cast do contrato (castExpression)
    # tem prioridade; o sidecar so preenche coluna SEM cast declarado. Ver
    # scripts/measure_casts.py (medido dos examples reais, nao por nome).
    _apply_measured_casts(catalog)

    orgaos = {}
    for key in catalog:
        orgao = key.split("/")[0]
        orgaos[orgao] = orgaos.get(orgao, 0) + 1
    for orgao, count in sorted(orgaos.items()):
        logger.info("%s: %d contratos", orgao, count)
    logger.info("Total: %d contratos ODCS", len(catalog))
    return catalog


def _apply_measured_casts(catalog: dict) -> None:
    """Sobrepoe casts do sidecar em colunas SEM cast declarado. Fail-safe:
    ausencia/erro do sidecar nao afeta o carregamento."""
    import json as _json

    side_path = CONTRACTS_DIR / "_measured_casts.json"
    if not side_path.exists():
        return
    try:
        side = _json.loads(side_path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        logger.warning("sidecar de casts ilegivel: %s", e)
        return
    aplicados = 0
    for key, casts in side.items():
        meta = catalog.get(key)
        if not meta:
            continue
        for col in meta.get("columns") or []:
            expr = casts.get(col.get("name"))
            if expr and not col.get("cast"):
                col["cast"] = expr
                aplicados += 1
    if aplicados:
        logger.info("casts medidos aplicados (sidecar): %d", aplicados)


def find(catalog: dict, nome: str, orgao: Optional[str] = None) -> Optional[dict]:
    """Busca dataset no catalogo. Se orgao nao informado, busca em todos."""
    if orgao:
        key = f"{orgao}/{nome}"
        if key in catalog:
            return catalog[key]
    # Busca em todos os orgaos
    for key, meta in catalog.items():
        if meta["name"] == nome or key.endswith(f"/{nome}"):
            return meta
    # Fuzzy
    nome_norm = nome.lower().replace("-", "_")
    for key, meta in catalog.items():
        if meta["name"].lower().replace("-", "_") == nome_norm:
            return meta
    return None


def format_schema(meta: dict) -> str:
    """Formata schema em YAML para prompt."""
    cols = meta.get("columns", [])
    if not cols:
        return ""
    lines = ["schema:"]
    pk = meta.get("primary_key", [])
    if pk:
        lines.append(f"  primary_key: [{', '.join(pk)}]")
    if meta.get("granularity"):
        lines.append(f"  granularity: {meta['granularity']}")
    lines.append("  columns:")
    for col in cols:
        lines.append(f"    - name: {col['name']}")
        lines.append(f"      type: {col['type']}")
        if col.get("unit"):
            lines.append(f"      unit: {col['unit']}")
        if col.get("semantic_type"):
            lines.append(f"      role: {col['semantic_type']}")
        lines.append(f"      description: {col['description']}")
        if col.get("enum"):
            lines.append(f"      valid_values: [{', '.join(str(e) for e in col['enum'][:8])}]")
        if col.get("cast"):
            lines.append(f"      cast: {col['cast']}")
        if col.get("aliases"):
            lines.append(f"      aliases: [{', '.join(col['aliases'][:5])}]")
    related = meta.get("related_datasets", {})
    if isinstance(related, dict) and related:
        lines.append("  related_datasets:")
        for rel_name, rel_data in related.items():
            if isinstance(rel_data, dict):
                lines.append(f"    - dataset: {rel_name}")
                if rel_data.get("join_keys"):
                    lines.append(f"      join_keys: [{', '.join(rel_data['join_keys'])}]")
    return "\n".join(lines)


def format_fewshots(meta: dict, n: int = 2) -> str:
    """Formata exemplos SQL/code para prompt."""
    fqs = meta.get("few_shot_queries", [])
    if not isinstance(fqs, list) or not fqs:
        return ""
    lines = []
    for fs in fqs[:n]:
        if isinstance(fs, dict):
            lines.append(f"Q: {fs.get('question', '')[:120]}")
            # Suporta tanto 'sql' quanto 'code'
            query = fs.get("sql") or fs.get("code", "")
            label = "SQL" if fs.get("sql") else "Code"
            lines.append(f"{label}: {query[:600]}")
            lines.append("")
    return "\n".join(lines)
