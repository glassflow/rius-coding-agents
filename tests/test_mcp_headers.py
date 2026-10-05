import json
import os
import subprocess
import pathlib

from rius_cc import config, login

SH = str(pathlib.Path(__file__).parent.parent / "scripts" / "mcp_headers.sh")
MCP_JSON = pathlib.Path(__file__).parent.parent / ".mcp.json"


def _headers(home, env=None):
    e = {"HOME": home, "PATH": os.environ["PATH"]}
    e.update(env or {})
    r = subprocess.run(["sh", SH], capture_output=True, text=True, env=e, timeout=30)
    return json.loads(r.stdout)


def test_stored_login_key_becomes_the_bearer(tmp_path):
    login._write_private(login.credentials_path(str(tmp_path)),
                         {"api_key": "ri_stored", "endpoint": "https://ingest.eu.console.rius-glassflow.com"})
    assert _headers(str(tmp_path)) == {"Authorization": "Bearer ri_stored"}


def test_an_env_key_is_ignored_as_it_is_for_tracing(tmp_path):
    login._write_private(login.credentials_path(str(tmp_path)),
                         {"api_key": "ri_stored", "endpoint": "https://ingest.eu.console.rius-glassflow.com"})
    assert _headers(str(tmp_path), {"RIUS_API_KEY": "ri_env"}) == {
        "Authorization": "Bearer ri_stored"}


def test_no_key_is_an_empty_object_not_prose(tmp_path):
    assert _headers(str(tmp_path)) == {}


def test_bundled_server_uses_the_helper():
    server = json.loads(MCP_JSON.read_text())["mcpServers"]["rius"]
    assert server["type"] == "http"
    assert "mcp_headers.sh" in server["headersHelper"]
    assert "headers" not in server  # a static header would bake in a key


def test_bundled_server_defaults_to_production_and_obeys_rius_mcp_url():
    url = json.loads(MCP_JSON.read_text())["mcpServers"]["rius"]["url"]
    assert url == "${RIUS_MCP_URL:-%s}" % config.DEFAULT_MCP_URL
    assert config.DEFAULT_MCP_URL == "https://mcp.eu.console.rius-glassflow.com/mcp"
