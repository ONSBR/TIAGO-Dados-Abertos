"""Configuracao centralizada do MCP TIAGO Dados Abertos."""

import os
from pathlib import Path

from mcp_tiago_dados_abertos import __version__

# Raiz do projeto (4 niveis acima de infra/config.py).
_PROJECT_DIR = Path(__file__).parent.parent.parent.parent
ENV_FILE = Path(os.getenv("MCP_ENV_FILE", str(_PROJECT_DIR / ".env")))

# Carrega o .env ANTES de ler qualquer variavel, para que os valores do arquivo
# cheguem a toda a configuracao abaixo. setdefault: variaveis ja presentes no
# ambiente do processo tem prioridade sobre o .env.
if ENV_FILE.exists():
    for _line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

# Limites de execucao DuckDB (anti-DoS de memoria/CPU + paginacao server-side)
DUCKDB_MEMORY_LIMIT = os.getenv("MCP_DUCKDB_MEMORY_LIMIT", "2GB")
QUERY_TIMEOUT_S = float(os.getenv("MCP_QUERY_TIMEOUT_S", "30"))
MAX_PAGE_LIMIT = int(os.getenv("MCP_MAX_PAGE_LIMIT", "1000"))

# Anti-abuso (trafego publico nao-confiavel). Teto agregado do processo: protege a
# fonte e a memoria, nao distingue origem. Limite por origem e' trabalho do proxy
# na frente — ver README.md, secao Configuracao.
RATE_LIMIT_GLOBAL_MAX = int(os.getenv("MCP_RATE_LIMIT_GLOBAL_MAX", "600"))  # queries/janela agregadas
RATE_LIMIT_MAX_KEYS = int(os.getenv("MCP_RATE_LIMIT_MAX_KEYS", "20000"))

# Onde estao os contratos, em ordem de preferencia:
#   1. dentro do pacote (mcp_tiago_dados_abertos/contracts) — e' o que o wheel embute,
#      entao `pip install` do pacote ja' vem com o corpus;
#   2. na raiz do checkout (./contracts) — instalacao editavel / clone;
#   3. MCP_CONTRACTS_DIR sobrepoe qualquer um dos dois.
_PACKAGE_DIR = Path(__file__).parent.parent
_DEFAULT_CONTRACTS = _PACKAGE_DIR / "contracts"
if not _DEFAULT_CONTRACTS.exists():
    _DEFAULT_CONTRACTS = _PROJECT_DIR / "contracts"
CONTRACTS_DIR = Path(os.getenv("MCP_CONTRACTS_DIR", str(_DEFAULT_CONTRACTS)))


# Servidor
HOST = os.getenv("MCP_HOST", "0.0.0.0")
PORT = int(os.getenv("MCP_PORT", "8003"))
VERSION = __version__  # fonte unica: mcp_tiago_dados_abertos.__init__

# ── Fonte de dados ────────────────────────────────────────────────────────────
# O ONS publica Parquet aberto em S3 (leitura anonima). O UA identifica o cliente
# nas leituras HTTPS (read_csv/read_json).
MCP_HTTP_UA = os.getenv("MCP_HTTP_UA", "mcp-tiago-dados-abertos/1.0 (contato@exemplo.com)")
