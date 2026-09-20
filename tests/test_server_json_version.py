"""server.json e' a segunda fonte de versao que o registry MCP le (e recusa versao repetida).
Tem que acompanhar __version__ - o publish so descobre a divergencia depois da tag."""
import json
from pathlib import Path

from mcp_tiago_dados_abertos import __version__

SERVER_JSON = Path(__file__).resolve().parents[1] / "server.json"


def test_server_json_version_matches_package():
    data = json.loads(SERVER_JSON.read_text(encoding="utf-8"))
    assert data["version"] == __version__
    assert [p["version"] for p in data["packages"]] == [__version__] * len(data["packages"])
