"""`--env local`: a Rius stack on this machine. It has no sign-in host, so a key
comes from `use-key` only, and nothing is ever sent to a sign-in host for it."""
import io

import pytest

import rius_ctl
from rius_cc import config, login
from tests.signed_in import sign_in

LOCAL_INGEST = "http://localhost:4318"
PROD_LINK_BASE = "https://connect.console.rius-glassflow.com"
LOCAL_KEY = "ri_test_not_a_key"
PROD_KEY = "glassflow_testkey"


class Recorder:
    """A `post` that records every call and answers 200."""
    def __init__(self):
        self.calls = []

    def __call__(self, url, payload, bearer=None):
        self.calls.append((url, payload, bearer))
        return 200, {}


def _forbidden(*args, **kwargs):
    raise AssertionError("a local environment must not make an HTTP call")


def test_use_key_stores_the_local_endpoint_and_it_is_the_one_sent_to(tmp_path):
    home = str(tmp_path)
    login.use_key(home, LOCAL_KEY, "local")
    stored = login.read_credentials(home)
    assert (stored["endpoint"], stored["env"]) == (LOCAL_INGEST, "local")
    resolved = config._credential(home)
    assert (resolved[0], resolved[1], resolved[-1]) == (LOCAL_KEY, LOCAL_INGEST, None)
    # Local credentials carry no workspace; nothing may claim one.
    cfg = config.resolve("s1", "/x", {}, home)
    assert (cfg.api_key, cfg.endpoint) == (LOCAL_KEY, LOCAL_INGEST)
    assert cfg.workspace_name is None


def test_switching_from_local_to_production_revokes_nothing(tmp_path):
    home = str(tmp_path)
    login.use_key(home, LOCAL_KEY, "local")
    post = Recorder()
    login.use_key(home, PROD_KEY, "production", post=post)
    assert post.calls == []
    stored = login.read_credentials(home)
    assert (stored["api_key"], stored["env"]) == (PROD_KEY, "production")


def test_switching_from_production_to_local_revokes_the_production_key(tmp_path):
    home = str(tmp_path)
    sign_in(home, api_key=PROD_KEY)
    post = Recorder()
    login.use_key(home, LOCAL_KEY, "local", post=post)
    assert post.calls == [(PROD_LINK_BASE + "/v1/agent-keys/revoke",
                           {"workspace_id": "ws-test"}, PROD_KEY)]
    assert login.read_credentials(home)["env"] == "local"


def test_revoking_a_local_key_is_a_no_op(tmp_path):
    creds = login.use_key(str(tmp_path), LOCAL_KEY, "local")
    assert login.revoke(creds, post=_forbidden) is False


def test_login_on_local_is_refused_before_any_http_call(tmp_path):
    home = str(tmp_path)
    with pytest.raises(login.LoginError, match="use-key"):
        login.start(home, "local", post=_forbidden)
    assert login.load_pending(home) is None


def test_login_wait_on_a_local_pending_link_is_refused_before_any_http_call(tmp_path):
    home = str(tmp_path)
    # Not something `start` can park; a hand-edited file must not get through.
    login._write_private(login.pending_path(home), {
        "env": "local", "link_id": "l1", "device_code": "dc_x",
        "user_code": "ABCD-EFGH", "connect_url": "http://localhost:3100/c",
        "interval": 0, "expires_at": 4102444800})
    with pytest.raises(login.LoginError, match="use-key"):
        login.wait(home, post=_forbidden, sleep=_forbidden)


@pytest.mark.parametrize("url, env, expected", [
    ("http://evil.example:4318", "local", False),
    ("https://ingest.eu.console.rius-glassflow.com", "local", False),
    ("http://localhost:4318", "local", True),
])
def test_only_this_machine_is_a_local_server(url, env, expected):
    assert login.is_rius_url(url, env) is expected


def test_the_login_command_says_how_to_store_a_local_key(tmp_path, capsys):
    rius_ctl.dispatch(["login", "--env", "local", "--cwd", "/p"], str(tmp_path))
    out = capsys.readouterr().out
    assert "The local Rius stack has no sign-in." in out
    assert "rius_ctl.sh use-key --env local`" in out
    assert login.load_pending(str(tmp_path)) is None


def test_the_refusal_names_the_agent_it_was_asked_by(tmp_path, capsys):
    rius_ctl.dispatch(["login", "--env", "local", "--agent", "codex",
                       "--cwd", "/p"], str(tmp_path))
    assert "rius_ctl.sh use-key --agent codex --env local`" in capsys.readouterr().out


def test_use_key_names_the_endpoint_for_every_environment(tmp_path, capsys,
                                                          monkeypatch):
    home = str(tmp_path)
    for env_name, environment in login.ENVIRONMENTS.items():
        monkeypatch.setattr("sys.stdin", io.StringIO(LOCAL_KEY))
        rius_ctl.dispatch(["use-key", "--env", env_name], home)
        out = capsys.readouterr().out
        assert "Stored the key for %s" % env_name in out
        assert "sent only to %s" % environment["ingest_url"] in out
        assert LOCAL_KEY not in out


def test_status_with_local_credentials_shows_the_local_endpoint(tmp_path, capsys):
    home = str(tmp_path)
    login.use_key(home, LOCAL_KEY, "local")
    rius_ctl.dispatch(["status", "--session", "s1", "--cwd", "/x"], home)
    out = capsys.readouterr().out
    assert "Endpoint: %s" % LOCAL_INGEST in out
    assert "Tracing: using the key from rius_ctl.sh use-key" in out
    assert "Workspace:" not in out
    assert LOCAL_KEY not in out
    # The bundled MCP server is production; say how to query the local one.
    assert rius_ctl.QUERYING_LOCAL_TRACES in out
    assert "rius-local http://localhost:8082/mcp" in out
    assert "staging" not in out
