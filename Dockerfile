FROM python@sha256:9534e5a8e315485d4061ed659af0fd78a284c015f9b73661b41d6bab25604534

WORKDIR /app

# Codigo primeiro: as dependencias vem do pyproject.toml, nao de uma segunda
# lista aqui dentro (duas listas divergem em silencio).
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir .
# Extensao httpfs do DuckDB na imagem: sem isto o primeiro boot baixa a extensao
# da internet, e um container sem saida de rede nao sobe. HOME=/app e o home do
# usuario de runtime, onde o DuckDB procura ~/.duckdb/extensions.
RUN HOME=/app python -c "import duckdb; duckdb.connect().execute('INSTALL httpfs')"

# Contratos ODCS: a fonte de verdade do servidor
COPY contracts/ ./contracts/
COPY scripts/ ./scripts/
# Cache pre-parseado: o boot carrega 1 JSON em vez de parsear os YAMLs um a um.
# Com 78 contratos economiza poucos segundos; a conta cresce com o corpus e o
# custo aqui e zero, porque roda no build e nao no boot.
RUN python scripts/build_contracts_cache.py --contracts-dir ./contracts

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MCP_CONTRACTS_DIR=/app/contracts \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8000

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

# Roda sem privilegio: o que o processo precisa escrever e' so o temp do DuckDB.
RUN useradd --system --uid 10001 --home-dir /app --shell /usr/sbin/nologin app \
    && chown -R app:app /app
USER app

CMD ["mcp-tiago-dados-abertos"]
