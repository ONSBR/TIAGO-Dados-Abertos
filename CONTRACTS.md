# Camada Semântica: Contratos ODCS

Este documento descreve a camada semântica do MCP TIAGO Dados Abertos: os contratos ODCS
que declaram schema, unidades, agregações válidas e instruções para o modelo de linguagem.

O contrato é a **fonte única de verdade**. O servidor só afirma o que o contrato sustenta.
Quando o contrato não declara uma unidade, um total ou uma fonte, o servidor omite; nunca
inventa.

## Estrutura de um contrato

Os contratos vivem em `contracts/ons/<nome>.odcs.yaml`, no padrão
[ODCS v3.1.0](https://github.com/bitol-io/open-data-contract-standard). A chave no catálogo
é o campo `name`; se ausente, usa o nome do arquivo.

### Campos obrigatórios (ODCS)

```yaml
version: 1.0.0
kind: DataContract
apiVersion: v3.1.0
id: urn:ons-opendata:meu-dataset
```

### Campos que o MCP lê

| Campo | Onde | Efeito |
|-------|------|--------|
| `name` | topo | Chave do dataset no catálogo |
| `status` | topo | `active` para aparecer no catálogo |
| `tags` | topo | Indexadas na busca |
| `description.purpose` | description | Descrição curta no catálogo |
| `description.limitations` | description | Avisos importantes sobre o dado |
| `description.usage` | description | Quando usar este dataset |
| `servers[].location` | servers | Origem S3 do dado |
| `schema[].properties[]` | schema | Colunas: nome, tipo, descrição |
| `customProperties` | topo | Metadados estendidos (ver abaixo) |

### customProperties que o MCP usa

| Property | Efeito |
|----------|--------|
| `granularity` | Grão temporal: horária, diária, mensal |
| `spatialGranularity` | Grão espacial: por_subsistema, por_usina, etc. |
| `aiContext` | Texto denso para a busca semântica |
| `vectorTags` | Sinônimos informais para a busca |
| `typicalQuestions` | Perguntas típicas que o dataset responde |
| `agentInstructions` | Instruções críticas para o modelo |
| `portalUrl` | URL no portal ONS (obrigatório para proveniência) |
| `sourceExpression` | Expressão de leitura do dado (sobrepõe location) |
| `partitionPruning` | Configuração de poda por partição |
| `temporalCoverageStart` | Início da cobertura temporal |
| `fewShotQueries` | Exemplos de SQL validados |

### Colunas (schema.properties)

Cada coluna pode ter:

```yaml
- name: val_carga
  physicalType: VARCHAR      # tipo no Parquet
  logicalType: number        # tipo semântico
  description: Carga verificada em MWmed
  customProperties:
  - property: castExpression
    value: TRY_CAST(val_carga AS DOUBLE)
  - property: unit
    value: MWmed
  - property: aliases
    value: [carga, demanda, consumo]
  - property: semanticType
    value: METRIC            # ou IDENTIFIER
```

## Informações perecíveis

O contrato descreve a **natureza** do dado, não fatos apurados em uma data.

### O que NÃO colocar na prosa

| Tipo | Exemplo ruim | Por quê |
|------|--------------|---------|
| Contagem absoluta | "13 em cerca de 39 mil linhas" | Muda a cada publicação |
| Percentual exato | "0,03% de nulos", "apenas 29,5%" | Idem |
| Data de apuração sem carimbo | "em 09/2026" | Parece fato permanente |

### O que colocar

**Faixas qualitativas:**
- `sem nulos` / `poucos nulos` / `nulos em parte das linhas` / `em boa parte` / `na maior parte`

**Avisos sobre padrões de ausência:**

```
Ausente em parte das linhas, quase sempre como string VAZIA e não como NULL —
um filtro IS NOT NULL não os exclui. Use NULLIF(coluna, '') ou coluna != ''.
```

### Guardas automatizadas

`tests/corpus/test_corpus_pereciveis.py` falha se encontrar:
- Contagem absoluta de linhas/registros
- Percentual de nulos no texto
- Percentual de cobertura fixo ("apenas N%")
- Data final congelada em série ativa
- Percentual entre parênteses após faixa qualitativa

Use `scripts/audit_null_bands.py` para medir a faixa real.

## Semântica (semantics)

O bloco `semantics` dentro de `customProperties` controla o que o servidor consegue provar:

```yaml
- property: semantics
  value:
    temporal_perspective: verificado  # ou programado, previsto, cadastral
    row_grain:
      temporal: diária
      spatial: subsistema
      temporal_column: din_instante
    metrics:
    - id: carga
      columns: [val_carga]
      expression: TRY_CAST(val_carga AS DOUBLE)
      unit_native: MWmed
      quantity_kind: power
      default_aggregation: sum
      forbidden_aggregations: []
      aggregation_modes:
        total_energy:
          sql: SUM(TRY_CAST(val_carga AS DOUBLE))
          unit: MWmed
    family: carga
    family_default: true
    measure_terms: [carga, demanda, consumo]
```

### Efeito de cada campo semântico

| Campo | Sem ele |
|-------|---------|
| `metrics[]` com `unit_native`, `aggregation_modes` | Não há unidade provada na resposta |
| `row_grain` | Servidor não sabe o que é uma linha; não decide aditividade |
| `temporal_perspective` | Pergunta neutra pode cair em dataset de previsão |
| `family` + `family_default` | Irmãos de grão diferente disputam ao acaso |
| `measure_terms` | "corte de geração" e "geração" caem no mesmo lugar |
| `sql_patterns` | Nenhuma consulta é rota `pattern` |

## Famílias de datasets

Quando há datasets que diferem só no grão (carga diária, horária, mensal), declare:

```yaml
semantics:
  family: carga
  family_default: true  # só no dataset que responde pergunta sem grão
```

A busca dá vantagem ao `family_default` quando a pergunta não menciona grão.

## Roteamento por termos de medida

Quando irmãos têm o mesmo ativo e medidas diferentes (corte, geração, disponibilidade):

```yaml
semantics:
  measure_terms: [corte, curtailment, restrição]
```

A busca soma vantagem quando a pergunta cita um desses termos.

Para a documentação completa do algoritmo de roteamento (tabela de pesos, detecção de
intenção, calibração e benchmark) veja [docs/ROUTING.md](docs/ROUTING.md).

## SQL patterns

Padrões SQL validados que o modelo pode copiar:

```yaml
- property: fewShotQueries
  value:
  - question: Qual a carga do Sudeste em agosto?
    sql: |
      SELECT SUM(TRY_CAST(val_carga AS DOUBLE)) AS carga_mwmed
      FROM read_parquet('s3://ons-aws-prod-opendata/dataset/carga_energia_di/*.parquet')
      WHERE id_subsistema = 'SE'
        AND din_instante >= '2024-08-01'
        AND din_instante < '2024-09-01'
```

## Validação de contratos

Antes de commitar:

```bash
python scripts/build_contracts_cache.py   # regera o cache
pytest tests/corpus -q                     # valida integridade e ODCS
python scripts/self_retrieval.py           # mede roteamento
```

Scripts de auditoria:

| Script | Pergunta |
|--------|----------|
| `audit_null_bands.py` | A faixa de nulos declarada ainda é válida? |
| `audit_row_grain.py` | O grão declarado bate com o grão das linhas? |
| `audit_interval_hours.py` | O intervalo temporal declarado está correto? |
| `audit_coverage_stale.py` | A cobertura temporal envelheceu? |
| `measure_casts.py` | Quais casts o dado exige? |

## Quality (futuro)

O formato correto para declarar qualidade de dados é o estruturado do ODCS:

```yaml
quality:
- type: library
  metric: nullValues
  column: val_carga
  mustBeLessThan: 5
  unit: percent
```

Não use blocos customizados com engine Soda. Eles não são executados pelo MCP.
