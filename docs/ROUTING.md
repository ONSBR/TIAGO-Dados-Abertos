# Roteamento de perguntas para datasets

Este documento descreve como a escolha do dataset acontece. É referência para quem
calibra contratos ou quer entender por que uma pergunta foi para determinado dataset.

## Duas formas de roteamento

O servidor oferece duas formas de escolher o dataset:

### 1. LLM escolhe: `listar_datasets`

O modelo lê o catálogo inteiro (~4 mil tokens) e escolhe pelo assunto e pelo grão. Cada
linha traz nome, granularidade, perspectiva temporal e uso, informação suficiente para
o modelo decidir sem ajuda do servidor.

```
listar_datasets() → modelo lê, escolhe, chama descrever_dataset(nome)
```

**Vantagem:** o modelo vê o contexto completo e pode aplicar raciocínio próprio.
**Desvantagem:** consome tokens do contexto; em catálogos muito grandes, pode não caber.

### 2. Servidor rankeia: `buscar_dataset`

O servidor faz o ranking (BM25 + keyword scoring) e devolve os candidatos mais
relevantes com faixa de relevância (alta/média/fraca) e os termos que casaram.

```
buscar_dataset(tema) → servidor rankeia, modelo confirma com descrever_dataset
```

**Vantagem:** economiza tokens; funciona com catálogos grandes.
**Desvantagem:** depende da qualidade dos contratos e da calibração.

### Fluxo recomendado

Para o catálogo atual (~78 datasets), `listar_datasets` é a entrada padrão. Use
`buscar_dataset` quando o catálogo for grande demais para ler de uma vez, ou para
localizar datasets por um termo específico (sigla, nome de coluna, entidade).

---

## Algoritmo de `buscar_dataset`

O ranking combina dois sinais:

1. **BM25 (lexical)**: busca textual sobre um "documento" montado a partir de cada
   contrato: nome, tags, vectorTags, aiContext, granularidade, perguntas típicas,
   descrições e aliases de coluna, e perguntas dos fewShotQueries.

2. **Keyword scoring**: regras determinísticas que somam ou subtraem pontos conforme
   campos do contrato casam com tokens da pergunta ou a pergunta expressa uma intenção
   específica (inventário, ranking, série temporal, perspectiva futura).

O score final é a soma dos dois. Empates são resolvidos alfabeticamente pelo nome do
dataset, garantindo ranking estável entre execuções.

## Tabela de pesos

### Sinais positivos

| Campo do contrato | Peso | Condição de disparo |
|-------------------|------|---------------------|
| Posição no BM25 | +25/20/15/10/5 | 1º a 5º colocado no ranking BM25 |
| `tags` + `vectorTags` | +3 por tag | Token da pergunta casa na tag |
| `aiContext` | +2 por token | Token casa no contexto |
| `name` | +4×n (n≥2) ou +2 (n=1) | Tokens casando no nome do dataset |
| `typicalQuestions` | +n (n≥2) | Tokens casando em pergunta típica |
| `columns[].enum` ou `examples` | +10 (uma vez) | Valor do enum/example citado na pergunta |
| `semantics.sql_patterns.*.validated_params` | +10 (uma vez) | Valor provado por execução citado |
| `semantics.measure_terms` | +6 | Termo da medida citado na pergunta |
| `semantics.family_default: true` | +9 | Pergunta não menciona grão temporal |
| `semantics.temporal_perspective` | +6 | Pergunta pede "programado/previsto" e dataset é futuro, ou pede "verificado" e dataset é verificado |
| `semantics.query_routing.supports_ranking: true` | +4 | Pergunta tem "top/maior/menor/ranking" |
| `semantics.query_routing.supports_time_series: true` | +3 | Pergunta tem "série/evolução/histórico" |
| `semantics.query_routing.primary_use_case: cadastro` | +8 | Pergunta de inventário ("quais existem") |
| `semantics.row_grain.temporal: STATIC` | +8 | Pergunta de inventário |

### Penalidades

| Condição | Peso | Motivo |
|----------|------|--------|
| Sem `granularity` e <5 colunas | −4 | Contrato incompleto |
| Sem coluna METRIC/unit + pergunta pede ranking | −6 | Não suporta agregação |
| `supports_ranking: false` + pergunta pede ranking | −4 | Declaração explícita |
| `supports_time_series: true` + pergunta de inventário | −8 | Série temporal para "quais existem" |
| Perspectiva futura + pergunta neutra | −6 | Programado/previsto sem pedido explícito |
| Perspectiva futura + pergunta pede verificado | −6 | Conflito de perspectiva |

## Detecção de intenção

### Inventário

Palavras que disparam: `quais`, `quantos`, `quantas`, `lista`, `listar`, `cadastro`,
`existem`, `inventário`, `relação`.

A intenção de inventário **não dispara** se a pergunta tiver marca de período (`ontem`,
`2024`, `último mês`, etc.). "Quais usinas geraram mais em 2024" é série temporal, não
inventário.

### Perspectiva temporal

| Pergunta contém | Dataset ganha | Dataset perde |
|-----------------|---------------|---------------|
| "programado", "previsto", "previsão", "DESSEM", "PMO", "amanhã" | `temporal_perspective: programado/previsto` | `temporal_perspective: verificado` |
| "verificado", "realizado", "medido", "efetivo" | `temporal_perspective: verificado` | `temporal_perspective: programado/previsto` |
| nenhum dos anteriores | `temporal_perspective: verificado` | `temporal_perspective: programado/previsto` |

A regra de perspectiva faz "carga prevista para amanhã" ir ao DESSEM e "geração 2025" ir
ao verificado (porque não pediu explicitamente previsão).

### Grão temporal

Palavras de grão: `hora`, `horário`, `dia`, `diário`, `ontem`, `semana`, `mês`, `mensal`,
`ano`, `anual`.

Se a pergunta **não menciona grão**, o dataset marcado como `family_default: true` ganha
+9 pontos. Isso faz "qual a carga do Sudeste" ir ao dataset diário (referência da família)
em vez do horário ou mensal.

## O que entra no BM25

O "documento" de cada contrato concatena:

- Nome do dataset (hífens e underscores viram espaços)
- `tags` e `vectorTags`
- `aiContext`
- `granularity` e `spatialGranularity`
- `typicalQuestions`
- `columns[].description` e `columns[].aliases`
- `fewShotQueries[].question`

Tokenização: minúsculas sem acento, tokens alfanuméricos de 2+ caracteres, mais bigramas
(`geracao_eolica`). Parâmetros BM25: k1=1.5, b=0.4.

## Como medir

O script `scripts/benchmark_roteamento.py` mede o ranking contra um gabarito de perguntas
com dataset correto anotado.

```bash
python scripts/benchmark_roteamento.py \
    --gabarito GOLDEN_ROUTING.json \
    --excluir conjunto_calibracao.json \
    --saida rodada.json
```

### Métricas

| Métrica | O que mede |
|---------|------------|
| top-1 | % de perguntas onde o dataset correto é o primeiro |
| top-3 | % onde o correto está entre os 3 primeiros |
| top-5/10 | Idem para 5 e 10 |
| MRR | Mean Reciprocal Rank: média de 1/posição |
| Fora do top-10 | % de perguntas sem o correto nos 10 primeiros |

### Comparação pareada

Com `--comparar anterior.json`, o script roda o teste de McNemar exato para cada corte
(top-1, top-3, top-10), mostrando quantas perguntas pioraram, melhoraram, e o p-valor.

### Pares de confusão

O benchmark lista os pares (esperado → devolvido) mais frequentes. Se `carga-energia` está
sempre confundindo com `balanco-energia-subsistema`, o contrato de um dos dois precisa de
diferenciação (tags, measure_terms, aiContext).

## Como calibrar um contrato novo

1. **Defina o assunto**: `tags` e `vectorTags` são a primeira linha de defesa. Use termos
   que o usuário diria, não jargão interno.

2. **Diferencie de vizinhos**: se há datasets parecidos (mesma grandeza, grão diferente),
   use `measure_terms` para a medida específica e `family`/`family_default` para o grão.

3. **Adicione perguntas típicas**: `typicalQuestions` e `fewShotQueries[].question` entram
   no BM25. Cubra as formas comuns de perguntar.

4. **Declare a perspectiva**: `temporal_perspective: verificado | programado | previsto`
   separa o histórico da previsão.

5. **Declare capacidades de roteamento**: `query_routing.supports_ranking`,
   `supports_time_series`, `supports_listing`, `primary_use_case`.

6. **Valide valores**: `sql_patterns.*.validated_params` e `columns[].enum` dão +10 quando
   a pergunta cita um valor que o dataset tem. "Geração de Tucuruí" vai direto ao dataset
   que lista "Tucuruí" como valor validado.

7. **Rode o benchmark**: compare top-1 e MRR antes e depois. Se piorou em perguntas que não
   são do seu dataset, você está roubando tráfego de outro. Reduza o ruído.

## Exemplos de ajustes

### MMGD no 4º lugar do BM25

**Problema**: "evolução da MMGD" ia para `balanco-energia` em vez de `micro-mini-geracao`.

**Diagnóstico**: BM25 colocava `micro-mini-geracao` em 4º lugar (score 0.51), mas o boost
só ia até o 3º colocado, e o 4º recebia zero.

**Solução**: estender o boost do BM25 até o 5º lugar (25/20/15/10/5 em vez de 18/12/6/0/0).

**Resultado**: top-3 subiu de 85.1% para 85.9% (p=0.04), sem degradar top-1.

### Inventário caindo em série temporal

**Problema**: "quais usinas existem" ia para dataset de geração (série temporal) e voltava
números de desempenho em vez da lista de usinas.

**Diagnóstico**: sem distinção entre cadastro e indicador, a pergunta casava nas tags de
geração e ia para o dataset errado.

**Solução**: adicionar detecção de intenção de inventário (`quais`, `quantos`, `lista`,
`existem`) com +8 para datasets marcados `primary_use_case: cadastro` ou
`row_grain.temporal: STATIC`, e −8 para séries temporais.

**Resultado**: perguntas de inventário passaram a ir para os cadastros.

### Substring "tem" casando "sistema"

**Problema**: "você tem dados sobre carga" ia para dataset errado porque "tem" (stopword
de conversa) casava como substring em "sistema", "subsistema", etc.

**Diagnóstico**: tokens curtos casavam por substring em palavras não relacionadas.

**Solução**: tokens com menos de 5 caracteres só casam palavra inteira (regex `\b`), não
substring. Criada lista de stopwords de conversa (`voce`, `tem`, `sobre`, `quero`, etc.).

**Resultado**: ruído de conversa parou de afetar o ranking.

### Perspectiva futura sem pedido explícito

**Problema**: "geração de energia 2025" ia para dataset programado/previsto, não verificado.

**Diagnóstico**: datasets de previsão competiam em pé de igualdade com o verificado, mesmo
quando a pergunta não pedia explicitamente previsão.

**Solução**: perspectiva futura (`programado`/`previsto`) recebe −6 em pergunta neutra.
Só ganha +6 se a pergunta pedir explicitamente (`previsão`, `programado`, `DESSEM`, etc.).

**Resultado**: "carga prevista para amanhã" vai ao DESSEM; "geração 2025" vai ao verificado.

## Histórico de calibração

| Data | Mudança | top-1 | top-3 | MRR |
|------|---------|-------|-------|-----|
| 2026-09-05 | BM25 substitui TF-IDF | 61% | — | — |
| 2026-09-07 | Boost BM25 até 5º lugar | — | 85.9% | — |

(Preencha conforme rodar novas calibrações)

## Referências

- Implementação: `src/mcp_tiago_dados_abertos/catalogo/ranking.py`
- Índice BM25: `src/mcp_tiago_dados_abertos/catalogo/retrieval.py`
- Benchmark: `scripts/benchmark_roteamento.py`
- Estrutura dos contratos: [CONTRACTS.md](../CONTRACTS.md)
