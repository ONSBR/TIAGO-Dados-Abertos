# Política de segurança

## Reportando uma vulnerabilidade

Não abra issue pública. Use o
[private vulnerability reporting](https://github.com/ONSBR/TIAGO-Dados-Abertos/security/advisories/new)
do GitHub.

Inclua o que acontece, como reproduzir e o impacto que você enxerga. A meta dos
mantenedores é um primeiro parecer em até 5 dias úteis.

Se o reporte privado não estiver disponível, use os contatos da organização
[ONSBR](https://github.com/ONSBR) no GitHub. Esse canal é só para vulnerabilidade. Questão
de conduta segue o [código de conduta](CODE_OF_CONDUCT.md); dúvida de uso vai numa issue
normal.

## Versões com correção de segurança

| versão | recebe correção |
|---|---|
| 1.x, a mais recente | sim |
| anteriores à mais recente | não; atualize |

## Divulgação

Divulgação coordenada. O aviso público sai junto com a versão corrigida, ou em 90 dias após
o reporte se a correção não estiver pronta, o que vier primeiro. Quem reportou é creditado no
aviso, salvo pedido em contrário. Pesquisa de boa-fé dentro do escopo abaixo não gera
denúncia por parte dos mantenedores.

## Superfície de ataque

O servidor executa SQL. As defesas que existem hoje:

- **Somente leitura, por construção.** O validador rejeita DML, DDL, `ATTACH`, funções de
  introspecção e leitura de arquivo local.
- **Allowlist de origem dos dados.** Só o bucket público do ONS,
  `s3://ons-aws-prod-opendata/`, e o domínio oficial `*.ons.org.br` por HTTPS. Qualquer
  outro host é bloqueado, e `http://` em claro também. É o que fecha SSRF via
  `read_csv('https://...')`.
- **A checagem falha fechada.** O caminho de um leitor tem que ser literal de string simples
  ou lista de literais. Caminho montado em tempo de execução, string `$$...$$` ou `E'...'`, e
  `FROM 'caminho'` sem função de leitor são recusados, não ignorados. A autoridade da URL não
  pode conter `@ # ? \ %`.
- **Leitores restritos.** Apenas `read_parquet`, `read_csv`, `read_csv_auto`, `read_json` e
  `read_json_auto`.
- **Sem controle do motor pelo SQL.** `INSTALL`, `LOAD`, `SET` e `PRAGMA` são rejeitados: o SQL
  não carrega extensão nem muda configuração do DuckDB, e uma instrução só por chamada.
- **Recursos limitados.** Tempo por consulta (`MCP_QUERY_TIMEOUT_S`), memória do DuckDB
  (`MCP_DUCKDB_MEMORY_LIMIT`) e tamanho de página (`MCP_MAX_PAGE_LIMIT`) têm teto.
- **Sem segredo no processo.** Não há credencial para vazar: a conexão DuckDB e a listagem
  de partições são anônimas, e o catálogo lê só origem pública.

Escapar de qualquer uma dessas, em especial ler um host fora da allowlist ou executar algo
que não seja `SELECT`, é vulnerabilidade.

O CI roda, em todo PR, auditoria de dependências (`pip-audit`), análise estática (Bandit) e
varredura de segredos (gitleaks); o Dependabot abre PRs para dependências e ações. A imagem
Docker roda sem privilégio e a base é fixada por digest.

## O que o servidor não faz sozinho

- **Limite por cliente.** O limite de consultas é do processo inteiro
  (`MCP_RATE_LIMIT_GLOBAL_MAX`, 600 por minuto, mais uma rajada fixa de 20 por segundo). Ele
  protege o bucket do ONS e a memória do servidor, e não distingue quem chama: o id de
  correlação vem do cabeçalho da requisição, e quem chama escolhe o que mandar. Quem expõe o
  modo HTTP publicamente precisa de limite por cliente no proxy ou gateway à frente.
- **Autenticação.** Não há. O transporte stdio é local por natureza; o HTTP presume que a
  rede ou o gateway na frente decidem quem chama.

## Antes de expor publicamente

- Troque `MCP_HTTP_UA`: o padrão traz `contato@exemplo.com`, um placeholder. É por ele que o
  ONS identifica quem consulta.
- Ponha limite por cliente no proxy à frente.
- Lembre que o modo HTTP não autentica ninguém: quem alcança a porta, consulta.

## Fora de escopo

- Indisponibilidade ou lentidão do portal do ONS.
- Conteúdo dos dados do ONS; este projeto não é a fonte deles.
- Consulta pesada que demora: existe timeout e paginação, mas o custo é do seu ambiente.
