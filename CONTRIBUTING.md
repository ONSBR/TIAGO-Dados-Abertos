# Contribuindo

Obrigado pelo interesse. Este projeto tem uma regra que vale mais que todas as outras:

> **O contrato é a fonte de verdade.** Se o comportamento está errado, quase sempre o
> conserto é no contrato, não no motor. Mudança no motor precisa valer para todos os
> datasets, não para o seu caso.

## Ciclo de desenvolvimento

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .\.venv\Scripts\activate
pip install -e ".[dev]"                   # ou: uv sync --all-extras, que usa o uv.lock
pre-commit install                        # opcional: o mesmo lint do CI, antes do commit
python scripts/build_contracts_cache.py   # o cache é artefato: regere sempre que um contrato mudar
pytest -q                                 # a suíte lê o catálogo real e roda sem internet
ruff check src/ tests/ scripts/           # lint, linha de 120
python scripts/self_retrieval.py          # roteamento: top-1 e top-3 por contrato, sem modelo
```

`self_retrieval.py` é o critério de aceite do roteamento: para cada contrato com
`typicalQuestions`, usa a primeira pergunta como consulta e vê em que posição o dataset
dono aparece. Enriquecer metadado só é ganho se esse número subir.

## Contribuindo com contratos

É o caminho mais útil, e não exige entender o motor. Os contratos vivem em
`contracts/ons/<nome>.odcs.yaml`, no padrão ODCS v3.1.0. A chave no catálogo é
`ons/<campo name>`; se `name` faltar, cai para o nome do arquivo.

### O mínimo que funciona

```yaml
version: 1.0.0
kind: DataContract
apiVersion: v3.1.0
id: urn:ons-opendata:meu-dataset
name: meu-dataset
tenant: ons
status: active
tags: [operacao-sistema, serie-temporal, diario]
description:
  purpose: O que este dado é, em uma frase.
servers:
- server: production
  type: s3
  location: s3://ons-aws-prod-opendata/dataset/meu_dataset/*.parquet
schema:
- name: meu_dataset
  properties:
  - name: din_instante
    physicalType: TIMESTAMP
    description: Instante da medida.
  - name: val_carga
    physicalType: DOUBLE
    unit: MWmed
    description: Carga verificada.
customProperties:
- property: ragContext
  value: Frase densa que descreve o dataset para a busca.
- property: granularity
  value: diária
- property: portalUrl
  value: https://dados.ons.org.br/dataset/meu-dataset
```

Não corte o `portalUrl`: sem ele o servidor omite o bloco de proveniência inteiro, porque
nunca inventa URL, e a atribuição exigida pela licença CC-BY do ONS deixa de sair.

### Os campos que mudam a resposta

Onde o dado está:

| campo | efeito |
|---|---|
| `servers[].location` com `type: s3` | a origem crua |
| `customProperties.sourceExpression` ou `parquetSource` | a expressão de leitura usada no SQL; sem ela, cai para a `location` |

Se a origem é um arquivo por período, escreva o placeholder na expressão: `{data_arquivo}`
para um arquivo por dia, `{mes_arquivo}` para um por mês. O motor resolve para o período
que a pergunta pede e recusa quando a pergunta não cabe na origem.

Em cada coluna de `properties[]`:

| campo | efeito |
|---|---|
| `physicalType` e `logicalType` | tipagem; `VARCHAR` físico com lógico numérico dispara aviso de `TRY_CAST` em `descrever_dataset` |
| `unit` | rótulo mostrado no schema. A unidade provada da resposta vem de `semantics.metrics`, não daqui |
| `description` | entra no documento de busca |
| `enum` e `examples` | valores válidos ou observados; a pergunta citar um deles pesa no roteamento |
| `customProperties.aliases` | sinônimos da coluna, que entram na busca |
| `customProperties.semanticType` | `METRIC` (a coluna é medida) ou `IDENTIFIER` (identifica a entidade; é o que uma contagem conta) |
| `customProperties.castExpression` | cast obrigatório antes de agregar, por exemplo número guardado como texto |
| `customProperties.aggregateMember` | o membro que já é o total (`SIN` entre subsistemas), para evitar dupla contagem. Só vale se a mesma coluna declarar `enum` |

O bloco `semantics` decide o que o servidor consegue afirmar:

| campo | sem ele |
|---|---|
| `semantics.metrics[]` com `unit_native`, `quantity_kind`, `aggregation_modes`, `interval_hours`, `default_aggregation`, `forbidden_aggregations` | não há unidade provada nem bloco `computed-facts`; toda coluna sai com `unit: null` |
| `semantics.row_grain` com `temporal`, `spatial`, `temporal_column` | o servidor não sabe o que é uma linha; não decide aditividade nem emite o `period` da proveniência |
| `semantics.sql_patterns` | nenhuma consulta é reconhecida como rota `pattern`, e a tool experimental não tem SQL validado para usar |
| `semantics.query_routing` | o motor não sabe se o dataset serve ranking, série ou comparação |
| `semantics.temporal_perspective` (`verificado`, `programado`, `previsto` ou `cadastral`) | uma pergunta neutra sobre uma grandeza pode cair num dataset de programação, e "previsto" não acha a previsão |
| `semantics.ambiguity_policy` | o motor responde onde deveria pedir esclarecimento |
| `customProperties.typicalQuestions` | o contrato não é medido: `self_retrieval.py` só avalia quem tem pergunta típica |
| `customProperties.partitionPruning` | uma pergunta de um ano varre o histórico inteiro |
| `periodoCoberturaInicio` e `periodoCoberturaFim` | `descrever_dataset` não mostra cobertura |

### O que a busca lê

O documento indexado de cada contrato é `name`, `tags`, `vectorTags`, `ragContext`,
`granularity`, `spatialGranularity`, `typicalQuestions`, as descrições e os `aliases` das
colunas e as perguntas de `fewShotQueries`. O campo mais barato e mais eficaz é
`vectorTags`: os sinônimos informais que o usuário usa e o jargão do setor não tem, como
"nível da água" ou "energia limpa".

Todo contrato precisa de `vectorTags`, e um lint cobra isso. Duas regras que a medição
impôs: o sinônimo não repete uma `tag` que já existe, porque não acrescenta vocabulário; e
quando a família tem irmãos (EAR e ENA por bacia, por REE, por reservatório e por
subsistema), o sinônimo carrega o recorte que os separa, senão um irmão rouba as perguntas
do outro. A pontuação por tag tem teto por contrato, então encher o campo não vale como
peso: vale como vocabulário.

### Curadoria do roteamento

`semantics.selection_policy` declara claims curados. O árbitro os consulta sempre, não só
em empate: se um claim cobre a pergunta inteira, ele vence o score da busca. Um claim que
só casa parte da pergunta não derruba um vencedor folgado.

```yaml
- property: semantics
  value:
    selection_policy:
      preferred_for:
      - mix de geracao por subsistema
      - balanco carga-geracao
      not_preferred_for:
      - usina
      - ranking_usinas
```

Um claim casa quando todos os seus tokens estão na pergunta. Entre dois que casam, vence o
mais específico. Em `not_preferred_for`, basta um termo casar para desqualificar o
candidato, pela mesma regra. Escreva claims que sejam a intenção, não a pergunta inteira:
claim longo demais nunca casa, claim de uma palavra captura tudo.

### Vocabulário fechado

O contrato só pode declarar campo que o servidor lê ou mostra. O que o servidor lê está
em `catalogo/catalog.py`; o que ele mostra, em `tools/descrever_dataset.py`. Campo que
ninguém lê envelhece sem que ninguém perceba e engorda o índice de busca. Campo novo entra
no mesmo PR que o código que o consome. Contrato gerado por outro programa passa por
`python scripts/enxugar_contrato.py`, que remove uma lista conhecida de campos sem uso;
um campo inédito é responsabilidade da revisão do PR.

Regere o cache antes de rodar a suíte: com um cache válido, o servidor não lê o YAML que
você acabou de mudar.

Scripts de auditoria de contrato:

| script | pergunta que responde |
|---|---|
| `audit_row_grain.py` | o grão declarado bate com o grão das linhas? |
| `audit_interval_hours.py` | o intervalo temporal declarado bate com o observado? |
| `audit_coverage_stale.py` | a cobertura declarada envelheceu? |
| `measure_casts.py` | quais casts o dado exige, medidos nos valores reais? |

Antes de abrir o PR de contrato: `pytest -q` verde, `ruff` limpo, `self_retrieval.py` sem
queda, e o contrato respondendo de ponta a ponta: `descrever_dataset` mostra o schema e
`executar_sql` devolve resultado com unidade e fonte.

## Contribuindo com o motor

Aqui a barra é mais alta, porque o motor decide por todos os datasets ao mesmo tempo.

- **Teste antes.** Todo PR de comportamento traz um teste que falha sem o conserto. Se o
  teste passa nos dois estados, ele não prova nada.
- **Diga o defeito, não a solução.** A mensagem do PR e o docstring do teste descrevem o que
  estava errado e como isso aparecia para o usuário.
- **Sem regressão silenciosa.** `pytest -q` verde e `self_retrieval.py` sem queda.

Os testes seguem as camadas do código: `tests/catalogo`, `tests/execucao`, `tests/tools`
e `tests/corpus`, este último para a integridade dos contratos.
`tests/test_arquitetura_camadas.py` garante que cada camada só importa das anteriores. Vários testes montam catálogos sintéticos com órgãos fictícios: é a forma de
provar que rótulo, fonte e filtro por órgão saem do contrato lido, não de uma constante no
código.

## Como o código entra

GitHub flow, sem branch de integração intermediária:

1. **`main` é sempre publicável.** Ninguém faz push direto nela. Todo código entra por pull
   request, e o merge exige o CI verde: testes e lint em `src/`, `tests/` e `scripts/`.
2. **Branch curta a partir da `main`**, um assunto por PR. Quem é de fora trabalha num fork;
   mantenedor pode usar branch no próprio repositório. Prefixo no nome ajuda a ler a lista:
   `fix/`, `feat/`, `docs/`, `test/`.
3. **Squash merge.** A `main` fica com um commit por PR, e o título do PR vira a mensagem
   desse commit.
4. **Título do PR no formato Conventional Commits**, no imperativo, dizendo o efeito:
   `feat: ...`, `fix: ...`, `docs: ...`, `test: ...`, `refactor: ...`. Mudança que quebra a
   interface das tools ou dos blocos de saída leva `!` (`feat!: ...`) e vai para uma versão
   maior.
5. **O modelo de PR** ([.github/PULL_REQUEST_TEMPLATE.md](.github/PULL_REQUEST_TEMPLATE.md))
   pergunta três coisas: o que muda, por quê, e como foi provado.

### Release

Semver a partir do `1.0.0`. A versão tem uma fonte só,
`src/mcp_tiago_dados_abertos/__init__.py`; o `pyproject.toml` lê de lá e o `/health`
reporta a mesma. Uma release é um PR que sobe essa versão e acrescenta a seção
correspondente no [CHANGELOG.md](CHANGELOG.md), seguido de uma tag `vX.Y.Z` na `main` e de
uma GitHub Release apontando para a seção. A revisão do PR confere que as duas existem.
Mudança incompatível na interface pública (nome ou parâmetro de tool, formato
dos blocos de saída, variável `MCP_*`) é versão maior; adição compatível é menor; conserto é
patch.

## Padrões

- `ruff check src/ tests/ scripts/` limpo; configuração no `pyproject.toml`, linha de 120.
- Comentário explica por quê, não o quê. Comentário que repete o código é ruído; o que
  registra a decisão, ou o defeito que a motivou, é o que sobrevive.
- Título do PR no formato da seção acima; é ele que vira o commit na `main`.

## O que não entra

- Chamada a modelo de linguagem no servidor. O raciocínio é do cliente MCP; o servidor é
  determinístico e auditável.
- Escrita de qualquer tipo: o validador bloqueia DML e DDL por construção.
- Host novo na allowlist de rede que não seja domínio oficial do ONS.

## Licença das contribuições

O código é distribuído sob a [Apache License 2.0](LICENSE). Ao abrir um PR, você concorda
que sua contribuição entra sob essa mesma licença, nos termos da seção 5 dela, sem
condições adicionais. Não é preciso assinar CLA.

## Segurança

Não abra issue pública para vulnerabilidade. Veja [SECURITY.md](SECURITY.md).
