<!-- Título no formato Conventional Commits, no imperativo: `fix: ...`, `feat: ...`, `docs: ...`.
     Ele vira a mensagem do commit na main (squash merge). -->

## O que muda

<!-- Uma ou duas frases. O que o usuário ou o mantenedor passa a ver de diferente. -->

## Por quê

<!-- O defeito ou a necessidade, não a solução. Se é um bug, como ele aparecia. -->

## Como foi provado

<!-- O teste que falha sem a mudança e passa com ela; a medição; o comando que rodou. -->

## Checklist

- [ ] `pytest -q` verde e `ruff check src/ tests/ scripts/` limpo
- [ ] Mudança de comportamento vem com teste que **falha sem o conserto**
- [ ] Se toca em contrato: `python scripts/self_retrieval.py` não piorou, e o cache foi
      regerado (`python scripts/build_contracts_cache.py`)
- [ ] Se muda a interface pública (tools, blocos de saída, variáveis `MCP_*`): título com
      `!` e nota no CHANGELOG
- [ ] Documentação atualizada onde a mudança aparece
