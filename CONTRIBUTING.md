# Contribuindo

Obrigado pelo interesse. Este projeto tem uma regra que vale mais que todas as outras:

> **O contrato é a fonte de verdade.** Se o comportamento está errado, quase sempre o
> conserto é no contrato, não no servidor. Mudança no servidor precisa valer para todos os
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

É o caminho mais útil, e não exige entender o servidor. Os contratos vivem em
`contracts/ons/<nome>.odcs.yaml`, no padrão ODCS v3.1.0.

A documentação completa da camada semântica está em [CONTRACTS.md](CONTRACTS.md):
- Estrutura do contrato e campos que o MCP lê
- Informações perecíveis (o que evitar)
- Bloco `semantics` e seu efeito no roteamento
- Famílias de datasets e termos de medida
- Validação e scripts de auditoria

O algoritmo de roteamento (tabela de pesos, calibração, benchmark) está em
[docs/ROUTING.md](docs/ROUTING.md).

### Checklist antes do PR

```bash
python scripts/build_contracts_cache.py   # regera o cache
pytest tests/corpus -q                     # valida integridade e ODCS
python scripts/self_retrieval.py           # mede roteamento (sem queda)
```

O contrato deve responder de ponta a ponta: `descrever_dataset` mostra o schema e
`executar_sql` devolve resultado com unidade e fonte.

## Contribuindo com o servidor

Aqui a barra é mais alta, porque o servidor decide por todos os datasets ao mesmo tempo.

- **Teste antes.** Todo PR de comportamento traz um teste que falha sem o conserto. Se o
  teste passa nos dois estados, ele não prova nada.
- **Diga o defeito, não a solução.** A mensagem do PR e o docstring do teste descrevem o que
  estava errado e como isso aparecia para o usuário.
- **Sem regressão silenciosa.** `pytest -q` verde e `self_retrieval.py` sem queda.

Os testes seguem as camadas do código: `tests/catalogo`, `tests/execucao`, `tests/resposta`,
`tests/tools` e `tests/corpus`, este último para a integridade dos contratos.
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
