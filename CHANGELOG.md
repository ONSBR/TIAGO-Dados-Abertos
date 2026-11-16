# Changelog

Formato baseado em [Keep a Changelog](https://keepachangelog.com/pt-BR/1.1.0/). O que o
servidor faz está no [README](README.md); aqui entram só as mudanças entre versões.

## [Não lançado]

## [1.1.1] — 2026-11-16

- Segurança: `fastmcp` 3.4.6 → 4.1.8, que corrige vulnerabilidades conhecidas da 3.4.6, e
  dependências travadas no `uv.lock` atualizadas para versões sem vulnerabilidades (#30, #34).
- Contrato `restricao_coff_eolica_usi`: `location` apontava para o dataset fotovoltaico; agora
  aponta para o eólico, o mesmo da `sourceExpression` (#37).

## [1.1.0] — 2026-09-20

- Novos contratos `coff_eolica_usi_intrasemihora` e `coff_fotovoltaica_intrasemihora`.
- Contratos de constrained-off e `ind_disponibilidade_fgeracao_uge_mensal` alinhados aos
  dicionários de dados atuais do portal (#16).

## [1.0.1] — 2026-09-08

Primeira publicação.
