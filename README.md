<!-- mcp-name: io.github.ONSBR/tiago-dados-abertos -->
<p align="center">
  <img src="https://raw.githubusercontent.com/ONSBR/TIAGO-Dados-Abertos/main/docs/assets/logo-onstec.png" alt="ONStec" width="180">
</p>

# MCP TIAGO Dados Abertos — ONS

[![tests](https://github.com/ONSBR/TIAGO-Dados-Abertos/actions/workflows/tests.yml/badge.svg)](https://github.com/ONSBR/TIAGO-Dados-Abertos/actions/workflows/tests.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)
[![License: Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-green.svg)](https://github.com/ONSBR/TIAGO-Dados-Abertos/blob/main/LICENSE)
[![MCP](https://img.shields.io/badge/MCP-Model%20Context%20Protocol-purple.svg)](https://modelcontextprotocol.io/)
[![DuckDB](https://img.shields.io/badge/DuckDB-1.0+-orange.svg)](https://duckdb.org/)
[![ODCS](https://img.shields.io/badge/ODCS-v3.1.0-blue.svg)](https://github.com/bitol-io/open-data-contract-standard)

Servidor [MCP](https://modelcontextprotocol.io/) que dá a um modelo de linguagem acesso aos
[dados abertos do ONS](https://dados.ons.org.br/), o Operador Nacional do Sistema Elétrico.

Uma **camada semântica** em YAML (padrão ODCS) declara schema, unidades e agregações
válidas de cada dataset. O servidor roteia, valida e executa; o modelo do cliente escreve
o SQL. Cada resposta traz unidade e fonte provadas pelo contrato; quando não consegue
provar, declara desconhecido em vez de deduzir.

A camada semântica reduz erros do modelo: entrega schema exato, tipos corretos e padrões
SQL validados. Nenhum modelo de linguagem roda no servidor: ele entrega o catálogo e os
contratos, valida e executa o SQL, e tudo o que devolve é determinístico e rastreável ao
contrato.

## O que dá errado sem a camada semântica

Para quem analisa o setor elétrico com um assistente de IA e precisa que o número esteja
certo, não apenas plausível.

Pedir a carga do Sudeste em agosto a um modelo com acesso aos dados abertos falha em
lugares que não são o SQL:

- **Escolher o dataset errado.** O portal publica recortes parecidos com granularidades
  diferentes: horário e diário, por subsistema e por usina. A consulta roda, devolve um
  número plausível, e responde outra pergunta. O catálogo declara granularidade,
  perspectiva e uso de cada dataset, para a escolha ser pelo grão e não pelo nome.
- **Escrever SQL contra um schema imaginado.** Coluna que não existe, tipo que não bate,
  data em texto comparada como texto. O contrato entrega o schema exato, os casts
  obrigatórios e padrões de SQL já validados antes de o modelo escrever a consulta.
- **Errar a unidade.** MWmed não é MWh, e a diferença não aparece no resultado. Cada
  coluna devolvida vem com a unidade que o contrato prova.
- **Agregar o que não se agrega.** Somar coluna que já é média, ou totalizar linhas que
  já são um total, dobra o valor sem erro nenhum. O contrato declara a aditividade de
  cada coluna, e ela acompanha a resposta.
- **Perder a procedência.** Número em relatório sem saber de onde veio. Cada resposta
  cita o dataset lido e a URL dele no portal.

A garantia não é que o modelo acerte o SQL: é que o servidor só afirma o que o contrato
sustenta. Quando não sustenta, marca o campo como desconhecido em vez de deduzir.

## O que o servidor devolve

O modelo escreve a pergunta; o servidor devolve dado, unidade e fonte, provados pelo contrato.

**Pergunta:** *Qual foi a carga do Sudeste em agosto de 2020?*

```sql
SELECT id_subsistema, ROUND(AVG(TRY_CAST(val_cargaenergiamwmed AS DOUBLE)), 1) AS carga_mwmed
FROM read_parquet('s3://ons-aws-prod-opendata/dataset/carga_energia_di/*.parquet')
WHERE id_subsistema = 'SE' AND din_instante >= '2020-08-01' AND din_instante < '2020-09-01'
GROUP BY id_subsistema
```

| id_subsistema | carga_mwmed |
|---------------|-------------|
| SE            | 34682.8     |

```result-schema
{"columns": [{"name": "carga_mwmed", "agg": "AVG", "sources": ["val_cargaenergiamwmed"],
              "quantity_kind": "average_power", "unit": "MWmed", "additive": false}]}
```

```tiago-dados-abertos-sources
{"sources": [{"dataset": "carga-energia", "portal_url": "https://dados.ons.org.br/dataset/carga-energia"}]}
```

Os exemplos usam 2020 de propósito: a fonte do ONS passa por consistência recorrente e revisa períodos recentes, então um recorte antigo é o que se mantém estável. O que o servidor garante em qualquer período é a **forma** da resposta: a unidade, a aditividade e a fonte, que vêm do contrato.

A unidade `MWmed` e a fonte `carga-energia` não vêm do modelo, vêm do contrato. Se o contrato não declarasse, os blocos não apareceriam. Os blocos acima mostram os campos principais; o `result-schema` real traz ainda `role`, `mode` e `unit_tier`, e uma entrada para cada coluna do resultado, inclusive as de dimensão.

**Pergunta:** *Qual era a energia armazenada por subsistema em agosto de 2020?*

```sql
SELECT nom_subsistema, ROUND(AVG(TRY_CAST(ear_verif_subsistema_mwmes AS DOUBLE)), 0) AS ear_mwmes
FROM read_parquet('s3://ons-aws-prod-opendata/dataset/ear_subsistema_di/*.parquet')
WHERE ear_data >= '2020-08-01' AND ear_data < '2020-09-01'
GROUP BY nom_subsistema
```

| nom_subsistema | ear_mwmes |
|----------------|-----------|
| SUDESTE        | 91461     |
| NORDESTE       | 40821     |
| SUL            | 11687     |
| NORTE          | 11346     |

```result-schema
{"columns": [{"name": "ear_mwmes", "agg": "AVG", "sources": ["ear_verif_subsistema_mwmes"],
              "quantity_kind": "energy_stock", "unit": "MWmes", "additive": false}]}
```

```tiago-dados-abertos-sources
{"sources": [{"dataset": "ear-diario-por-subsistema", "portal_url": "https://dados.ons.org.br/dataset/ear-diario-por-subsistema"}]}
```

O `additive: false` avisa que estes valores não se somam **ao longo do tempo**: energia armazenada é estoque, uma foto do reservatório naquele dia. Somar o que havia em agosto com o que havia em setembro contaria a mesma água duas vezes. Somar os quatro subsistemas no mesmo dia é outra coisa, e é válido: dá o armazenamento do SIN.

## Por que este servidor e não consulta direta?

| Aspecto | Sem o servidor | Com o servidor |
|---------|----------------|----------------|
| Preparação | Baixar CSV, tratar em pandas/Excel, converter tipos | Pergunta direto; o servidor lê o S3 público |
| Escolha do dataset | O modelo adivinha pelo nome | Catálogo com granularidade, perspectiva e uso |
| Schema | O modelo inventa colunas | Contrato entrega o schema exato |
| Tipos | Cast errado falha em silêncio | `TRY_CAST` obrigatório, declarado no contrato |
| Unidade | Não se sabe se é MWh ou MWmed | Unidade provada pelo contrato |
| Dupla contagem | Somar SIN e subsistemas dobra o valor | Contrato avisa sobre a linha agregada |
| Procedência | Sem rastro | Cada resposta cita o dataset e a URL |
| Segurança | SQL arbitrário | Validador falha fechado, só leitura |

**Nota:** O servidor entrega o contexto certo (schema, métricas, fonte). A qualidade do
SQL e da interpretação depende do modelo do cliente. LLMs ainda erram; o servidor reduz
a superfície de erro, não a elimina.

## Como funciona

O contrato de cada dataset declara também granularidade, casts obrigatórios e as
perguntas que aquele dataset responde. É a única fonte de verdade: não existe configuração
paralela, e tudo o que o servidor afirma sai de um campo do contrato.

```mermaid
flowchart LR
    C["Cliente MCP<br/>(o modelo raciocina aqui)"] -->|lê o catálogo| L["listar_datasets<br/>catálogo inteiro<br/>(também resource)"]
    C -.->|filtro por termo| B["buscar_dataset<br/>ranking dos contratos"]
    L --> D["descrever_dataset<br/>schema, casts, padrões SQL"]
    B -.-> D
    D -->|o modelo escreve o SQL| E["executar_sql<br/>validador falha fechado"]
    E --> K["DuckDB<br/>zero ETL"]
    K <-->|Parquet| S3[("S3 público<br/>do ONS")]
    Ct[("contracts/<br/>ODCS")] -.-> L & B & D & E
```

- **Roteamento pelo catálogo.** `listar_datasets` entrega o catálogo inteiro, uma linha por
  dataset com granularidade, perspectiva e uso, e o modelo escolhe pelo assunto e pelo grão.
  `buscar_dataset` filtra por termo, com palavra-chave e BM25, para catálogo grande.
  `descrever_dataset` mostra o schema, as unidades e os avisos que o modelo precisa antes
  de escrever SQL.
- **Execução segura.** `executar_sql` aceita só leitura, só das origens do ONS, e devolve o
  resultado com a unidade e a aditividade de cada coluna provadas pelo contrato.
- **Só afirma o que prova.** Quando o contrato não sustenta uma unidade ou uma
  agregação, o servidor devolve `unit: null` e `unit_tier: "unknown"` em vez de deduzir
  um valor plausível. Fonte sem URL canônica no contrato é omitida do bloco de
  proveniência. Abstenção declarada, não erro.
- **Sem cópia dos dados.** O DuckDB lê o bucket público do ONS no momento da consulta, de
  forma anônima.

Cada resposta de `executar_sql` é a tabela em markdown seguida de dois blocos JSON:
`result-schema`, com a unidade e a aditividade de cada coluna e o tier do contrato que as
provou; e `tiago-dados-abertos-sources`, com o dataset lido e a URL no portal. Bloco que o
contrato não sustenta não aparece.

## Usar sem instalar

O servidor está disponível publicamente em:

```
https://mcp.dados.tiago.ons.org.br/
```

Veja instruções para cada cliente em [dados.ons.org.br/mcp](https://dados.ons.org.br/mcp).

## Instalação

Requisitos: Python 3.11 ou mais recente e acesso à internet. O bucket do ONS é público e
lido anonimamente. O pacote não está no PyPI; a instalação é por clone.

```bash
git clone https://github.com/ONSBR/TIAGO-Dados-Abertos.git
cd TIAGO-Dados-Abertos
python -m venv .venv

# Linux/macOS
source .venv/bin/activate
# Windows
.\.venv\Scripts\activate

pip install -e ".[dev]"
python scripts/build_contracts_cache.py   # acelera o boot; opcional
```

### Rodar

```bash
# STDIO (para IDEs e Claude Desktop)
mcp-tiago-dados-abertos --stdio

# HTTP
mcp-tiago-dados-abertos
# MCP:      http://localhost:8003/mcp
# health:   http://localhost:8003/health
# catálogo: http://localhost:8003/datasets
```

### Docker

```bash
docker build -t mcp-tiago-dados-abertos .
docker run -p 8000:8000 mcp-tiago-dados-abertos
```

Os contratos vão embutidos na imagem e o cache é gerado no build. A porta é 8003 no run
local e 8000 no container: o endpoint MCP do container fica em `http://localhost:8000/mcp`
e o health em `http://localhost:8000/health`. A imagem traz um `HEALTHCHECK` que consulta
esse endpoint a cada 30 segundos.

### Configurar no cliente MCP

Troque `<ABS_PATH>` pelo caminho absoluto do repositório. Em Linux/macOS o comando é
`<ABS_PATH>/.venv/bin/mcp-tiago-dados-abertos`. O arquivo
[`examples/mcp.json`](https://github.com/ONSBR/TIAGO-Dados-Abertos/blob/main/examples/mcp.json) tem o bloco pronto para copiar.

| cliente | onde | forma |
|---|---|---|
| Claude Desktop | `claude_desktop_config.json` | `mcpServers` com `command` e `args` |
| Claude Code | terminal | `claude mcp add tiago-dados-abertos -- <ABS_PATH>/.venv/Scripts/mcp-tiago-dados-abertos.exe --stdio` |
| Cursor | `.cursor/mcp.json` | `mcpServers` com `command` e `args` |
| Kiro | `.kiro/settings/mcp.json` | `mcpServers` com `command` e `args` |
| Windsurf | `~/.codeium/windsurf/mcp_config.json` | `mcpServers` com `command` e `args` |
| Gemini CLI | `~/.gemini/settings.json` | `mcpServers` com `command` e `args` |
| VS Code | `.vscode/mcp.json` | `servers` com `"type": "stdio"`, `command` e `args` |

Para os clientes com `mcpServers`:

```json
{
  "mcpServers": {
    "tiago-dados-abertos": {
      "command": "<ABS_PATH>/.venv/Scripts/mcp-tiago-dados-abertos.exe",
      "args": ["--stdio"]
    }
  }
}
```

Para o VS Code:

```json
{
  "servers": {
    "tiago-dados-abertos": {
      "type": "stdio",
      "command": "<ABS_PATH>/.venv/Scripts/mcp-tiago-dados-abertos.exe",
      "args": ["--stdio"]
    }
  }
}
```

Com o servidor em modo HTTP ou no container, Cursor, Kiro, VS Code e Claude Code aceitam a
URL no lugar de `command` e `args`: `"url": "http://localhost:8000/mcp"` (porta `8003` fora do
container), no VS Code com `"type": "http"`, e no Claude Code
`claude mcp add --transport http tiago-dados-abertos http://localhost:8000/mcp`.

## Tools

| tool | o que faz |
|---|---|
| `listar_datasets` | o catálogo inteiro, um dataset por linha, com granularidade, perspectiva temporal e uso; a entrada padrão do fluxo |
| `buscar_dataset` | filtra o catálogo por termo, com palavra-chave e BM25; para catálogo grande ou termo específico |
| `descrever_dataset` | schema, métricas, unidades, avisos de tipo, patterns SQL e exemplos |
| `executar_sql` | executa SQL DuckDB somente leitura, paginado, com unidade, fatos e fonte provados |

O conteúdo de `listar_datasets` também é o resource `catalogo://datasets`, para clientes que
fixam resources no contexto.

Fluxo recomendado:

```
1. listar_datasets()                    → lê o catálogo e escolhe pelo assunto e pelo grão
2. descrever_dataset("carga-energia")   → lê schema, unidades e avisos
3. executar_sql("SELECT ...")           → executa com as colunas reais
```

Nunca gere SQL sem ler o schema antes. As colunas têm os nomes do ONS, não os que parecem
óbvios, e várias métricas exigem o cast declarado no contrato.

## Configuração

Tudo é variável de ambiente. Defina no ambiente do processo, no bloco `env` do cliente MCP,
ou num arquivo `.env` na raiz do repositório, que o servidor carrega no boot. Variável já
definida no ambiente vence a do arquivo. Nenhuma é obrigatória. Há um
[`.env.example`](https://github.com/ONSBR/TIAGO-Dados-Abertos/blob/main/.env.example) com todas comentadas.

**Servidor**

| variável | padrão | efeito |
|---|---|---|
| `MCP_HOST` | `0.0.0.0` | endereço de bind no modo HTTP |
| `MCP_PORT` | `8003` | porta no modo HTTP (o Dockerfile fixa `8000`) |
| `MCP_STATELESS` | `true` | sessões HTTP sem estado; deixe `true` atrás de balanceador |
| `MCP_ENV_FILE` | `.env` na raiz do repositório | caminho do arquivo `.env`; num pacote instalado, aponte explicitamente |
| `MCP_CONTRACTS_DIR` | os contratos embutidos no pacote | só para apontar outro diretório de contratos; um `contracts_cache.json` válido na raiz dele substitui a leitura dos YAML |

**Execução de SQL**

| variável | padrão | efeito |
|---|---|---|
| `MCP_QUERY_TIMEOUT_S` | `30` | segundos até o servidor interromper a consulta e sugerir reduzir o escopo |
| `MCP_DUCKDB_MEMORY_LIMIT` | `2GB` | teto de memória do DuckDB |
| `MCP_MAX_PAGE_LIMIT` | `1000` | maior `limit` aceito em `executar_sql`; pedidos acima são reduzidos |
| `MCP_MAX_SQL_CHARS` | `65536` | maior SQL aceito em `executar_sql`, em caracteres; acima disso a consulta é recusada antes de qualquer análise |
| `MCP_HTTP_UA` | `mcp-tiago-dados-abertos/1.0 (contato@exemplo.com)` | `User-Agent` das leituras HTTPS. Troque o contato antes de expor o servidor: é como o ONS identifica quem consulta |

A conexão com o DuckDB é anônima de propósito e não recebe credencial AWS. O bucket do ONS
é público, e a ausência de chave é o que impede um SQL injetado de ler um bucket privado da
sua conta. Não acrescente credencial. A região é fixa em `us-west-2`, onde o bucket do ONS
vive.

**Limites de abuso**

| variável | padrão | efeito |
|---|---|---|
| `MCP_RATE_LIMIT_GLOBAL_MAX` | `600` | consultas por minuto no processo inteiro, somando todos os clientes |
| `MCP_RATE_LIMIT_MAX_KEYS` | `20000` | teto de chaves distintas no limitador, contra crescimento de memória |

O limite é do processo, mais uma rajada fixa de 20 consultas por segundo. Não há limite por
cliente: o servidor não distingue quem chama. Se você expuser o modo HTTP para além da sua
rede, ponha um limite por cliente no proxy ou gateway à frente.

**Datas e diagnóstico**

| variável | padrão | efeito |
|---|---|---|
| `MCP_DATA_TZ` | `America/Sao_Paulo` | fuso em que "hoje", "ontem" e "mês passado" são resolvidos. É o fuso dos dados, não do servidor, e aparece no rodapé de contexto temporal de toda resposta |
| `MCP_CORR_HEADER` | `x-request-id` | cabeçalho HTTP de onde vem o id de correlação da requisição; atrás de um proxy que gera o próprio id, aponte para o cabeçalho dele |
| `MCP_COBERTURA_PROBE` | vazio | `1` faz `descrever_dataset` consultar o S3 para descobrir a data mais recente de uma série ativa, com cache de uma hora por dataset e desistência em 8 segundos. Desligado por padrão |

## Estrutura do repositório

```
TIAGO-Dados-Abertos/
├── src/mcp_tiago_dados_abertos/
│   ├── tools/           # 4 tools MCP (buscar, listar, descrever, executar_sql)
│   ├── catalogo/        # carrega contratos, BM25, ranking
│   ├── execucao/        # validador SQL, poda de glob por ano, DuckDB, rate limiter
│   ├── resposta/        # result-schema, aditividade, proveniência, aviso de unidade, rodapé temporal
│   ├── infra/           # config, telemetria, correlação
│   └── server.py        # entrypoint MCP (tools + resource catalogo://datasets)
├── contracts/ons/       # contratos ODCS (camada semântica), um por dataset
├── tests/               # catalogo, execucao, tools e corpus (integridade dos contratos)
├── scripts/             # build_contracts_cache.py
├── docs/                # ROUTING.md e assets do README
└── examples/            # mcp.json de exemplo
```

## Contribuir e reportar

Como contribuir com contratos ou com o código: [CONTRIBUTING.md](https://github.com/ONSBR/TIAGO-Dados-Abertos/blob/main/CONTRIBUTING.md).
Camada semântica e estrutura dos contratos: [CONTRACTS.md](https://github.com/ONSBR/TIAGO-Dados-Abertos/blob/main/CONTRACTS.md).
Algoritmo de roteamento e calibração: [docs/ROUTING.md](https://github.com/ONSBR/TIAGO-Dados-Abertos/blob/main/docs/ROUTING.md).
Vulnerabilidade: [SECURITY.md](https://github.com/ONSBR/TIAGO-Dados-Abertos/blob/main/SECURITY.md). Mudanças entre versões:
[CHANGELOG.md](https://github.com/ONSBR/TIAGO-Dados-Abertos/blob/main/CHANGELOG.md).

## Licença

O código é Apache 2.0: [LICENSE](https://github.com/ONSBR/TIAGO-Dados-Abertos/blob/main/LICENSE). Os dados são do ONS, sob CC-BY, e seguem os termos do
[Portal de Dados Abertos do ONS](https://dados.ons.org.br/); ver [NOTICE](https://github.com/ONSBR/TIAGO-Dados-Abertos/blob/main/NOTICE). Este
projeto não redistribui dados: ele lê a origem oficial no momento da consulta.
