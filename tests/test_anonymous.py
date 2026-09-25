"""Anonymous provisioning and claim (P-RIUS-196, model B).

The CLI tests run `rius_ctl.dispatch` in-process against a fake Rius/Auth0
that replaces `login._send`, the one place the plugin touches the network.
"""
import json
import os
import stat
import urllib.parse

import pytest

import rius_ctl
from rius_cc import anonymous, config, login

API_BASE = login.ENVIRONMENTS["staging"]["api_base"]
TOKEN = "ric_c2VjcmV0Y2xhaW10b2tlbnNlY3JldGNsYWltdG9rZW4"
CLAIM_URL = API_BASE + "/claim#" + TOKEN

PROVISION_RESPONSE = {
    "key": "ri_anonkey.secretpart", "workspace_id": "w-anon",
    "workspace_name": "Default", "scopes": ["ingest", "read"],
    "expires_at": "2027-09-25T00:00:00Z", "claim_token": TOKEN,
    "claim_url": CLAIM_URL,
}
TARGET_A = {"workspace_id": "w-a", "workspace_name": "Default",
            "org_name": "Kiran's org", "role": "admin"}
TARGET_B = {"workspace_id": "w-b", "workspace_name": "Team",
            "org_name": "GlassFlow", "role": "member"}
DEVICE_CODE_RESPONSE = {
    "device_code": "dev-secret", "user_code": "ABCD-EFGH",
    "verification_uri": "https://auth.example/activate",
    "verification_uri_complete": "https://auth.example/activate?user_code=ABCD-EFGH",
    "interval": 0, "expires_in": 900,
}


class FakeServer:
    """Answers by (method, path); each route holds a queue of replies."""

    def __init__(self, routes):
        self.routes = {k: list(v) for k, v in routes.items()}
        self.calls = []

    def __call__(self, req, timeout=15.0):
        path = urllib.parse.urlparse(req.full_url).path
        key = (req.get_method(), path)
        body = req.data
        if body and req.get_header("Content-type") == "application/json":
            body = json.loads(body.decode("utf-8"))
        self.calls.append({"key": key, "url": req.full_url, "body": body,
                           "auth": req.get_header("Authorization")})
        if key not in self.routes or not self.routes[key]:
            raise AssertionError("unexpected request %s %s" % key)
        return self.routes[key].pop(0)

    def count(self, method, path):
        return sum(1 for c in self.calls if c["key"] == (method, path))


@pytest.fixture
def env(monkeypatch):
    monkeypatch.delenv("RIUS_API_KEY", raising=False)
    monkeypatch.delenv("RIUS_ENDPOINT", raising=False)
    monkeypatch.setattr(rius_ctl, "_open_browser", lambda url: None)


def serve(monkeypatch, routes):
    fake = FakeServer(routes)
    monkeypatch.setattr(login, "_send", fake)
    return fake


def store_anonymous(home, **overrides):
    creds = {"api_key": "ri_anonkey.secretpart", "anonymous": True,
             "claim_token": TOKEN, "claim_url": CLAIM_URL,
             "endpoint": login.ENVIRONMENTS["staging"]["ingest_endpoint"],
             "env": "staging", "workspace_id": "w-anon",
             "workspace_name": "Default", "scopes": ["ingest", "read"]}
    creds.update(overrides)
    login._write_private(login.credentials_path(home), creds)


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def run(home, *argv):
    rius_ctl.dispatch(list(argv), home)


PROVISION = ("POST", "/v1/anonymous/workspaces")
TARGETS = ("GET", "/v1/claims/targets")
CLAIM = ("POST", "/v1/claims")
DEVICE_CODE = ("POST", "/oauth/device/code")
AUTH0_TOKEN = ("POST", "/oauth/token")


# --- provision ---------------------------------------------------------------

def test_enable_here_without_a_key_provisions_once_and_stores_privately(
        tmp_path, monkeypatch, env, capsys):
    home = str(tmp_path)
    fake = serve(monkeypatch, {PROVISION: [(201, PROVISION_RESPONSE)]})
    run(home, "enable-here", "--cwd", "/opt/proj")

    assert fake.count(*PROVISION) == 1
    assert fake.calls[0]["url"] == API_BASE + "/v1/anonymous/workspaces"
    assert fake.calls[0]["auth"] is None
    path = login.credentials_path(home)
    assert mode(path) == 0o600
    creds = json.load(open(path))
    assert creds["anonymous"] is True
    assert creds["api_key"] == "ri_anonkey.secretpart"
    assert creds["claim_token"] == TOKEN
    assert creds["claim_url"] == CLAIM_URL
    assert creds["endpoint"] == login.ENVIRONMENTS["staging"]["ingest_endpoint"]
    assert creds["workspace_id"] == "w-anon"
    out = capsys.readouterr().out
    assert "Tracing on. Claim this workspace any time: %s" % CLAIM_URL in out


def test_enable_here_with_rius_api_key_never_provisions(
        tmp_path, monkeypatch, env, capsys):
    home = str(tmp_path)
    monkeypatch.setenv("RIUS_API_KEY", "ri_envkey")
    fake = serve(monkeypatch, {})
    run(home, "enable-here", "--cwd", "/opt/proj")
    assert fake.calls == []
    assert not os.path.exists(login.credentials_path(home))


def test_enable_here_with_a_stored_key_never_provisions(
        tmp_path, monkeypatch, env):
    home = str(tmp_path)
    login._write_private(login.credentials_path(home),
                         {"api_key": "ri_stored", "endpoint": "https://x"})
    fake = serve(monkeypatch, {})
    run(home, "enable-here", "--cwd", "/opt/proj")
    assert fake.calls == []


def test_provision_rate_limited_stores_nothing_but_keeps_the_folder_enabled(
        tmp_path, monkeypatch, env, capsys):
    home = str(tmp_path)
    serve(monkeypatch, {PROVISION: [(429, {"title": "Too Many Requests"})]})
    run(home, "enable-here", "--cwd", "/opt/proj")
    out = capsys.readouterr().out
    assert "try again later" in out
    assert not os.path.exists(login.credentials_path(home))
    rules = json.load(open(config.path_rules_path(home)))
    assert "/opt/proj" in rules["enabled_paths"]


def test_provision_unreachable_stores_nothing(tmp_path, monkeypatch, env, capsys):
    home = str(tmp_path)

    def offline(req, timeout=15.0):
        raise OSError("no route to host")
    monkeypatch.setattr(login, "_send", offline)
    run(home, "enable-here", "--cwd", "/opt/proj")
    assert "could not reach" in capsys.readouterr().out.lower()
    assert not os.path.exists(login.credentials_path(home))


def test_provision_action_provisions_for_headless_use(
        tmp_path, monkeypatch, env, capsys):
    home = str(tmp_path)
    fake = serve(monkeypatch, {PROVISION: [(201, PROVISION_RESPONSE)]})
    run(home, "provision")
    assert fake.count(*PROVISION) == 1
    assert json.load(open(login.credentials_path(home)))["anonymous"] is True
    assert CLAIM_URL in capsys.readouterr().out
    assert not os.path.exists(config.path_rules_path(home))


def test_provision_action_refuses_with_rius_api_key(
        tmp_path, monkeypatch, env, capsys):
    monkeypatch.setenv("RIUS_API_KEY", "ri_envkey")
    fake = serve(monkeypatch, {})
    run(str(tmp_path), "provision")
    assert fake.calls == []
    assert "RIUS_API_KEY" in capsys.readouterr().out


def test_provision_action_keeps_an_existing_key(tmp_path, monkeypatch, env, capsys):
    home = str(tmp_path)
    store_anonymous(home)
    fake = serve(monkeypatch, {})
    run(home, "provision")
    assert fake.calls == []
    assert CLAIM_URL in capsys.readouterr().out


def test_provision_rejects_a_response_without_a_claim_token(tmp_path):
    body = dict(PROVISION_RESPONSE)
    del body["claim_token"]
    with pytest.raises(login.LoginError):
        anonymous.provision(str(tmp_path), post=lambda url, payload: (201, body))
    assert not os.path.exists(login.credentials_path(str(tmp_path)))


# --- status, redact, logout --------------------------------------------------

def test_status_shows_the_claim_url_and_the_token_nowhere_else(
        tmp_path, monkeypatch, env, capsys):
    home = str(tmp_path)
    store_anonymous(home)
    run(home, "status", "--session", "s1", "--cwd", "/x")
    out = capsys.readouterr().out
    assert "Workspace: unclaimed. Claim: %s" % CLAIM_URL in out
    assert out.count(TOKEN) == out.count(CLAIM_URL) == 1
    assert "secretpart" not in out


def test_redact_never_prints_a_claim_token():
    assert "ric_" not in config.redact(TOKEN)
    assert TOKEN[4:12] not in config.redact(TOKEN)


def test_logout_of_an_unclaimed_credential_warns_with_the_claim_url(
        tmp_path, monkeypatch, env, capsys):
    home = str(tmp_path)
    store_anonymous(home)
    run(home, "logout")
    out = capsys.readouterr().out
    assert CLAIM_URL in out
    assert "only way" in out
    assert not os.path.exists(login.credentials_path(home))


def test_logout_of_a_claimed_credential_prints_no_claim_url(
        tmp_path, monkeypatch, env, capsys):
    home = str(tmp_path)
    store_anonymous(home, anonymous=False)
    run(home, "logout")
    assert "/claim" not in capsys.readouterr().out


# --- claim through login -----------------------------------------------------

def login_routes(targets, *claims):
    return {DEVICE_CODE: [(200, DEVICE_CODE_RESPONSE)],
            AUTH0_TOKEN: [(200, {"access_token": "auth0-tok", "expires_in": 86400})],
            TARGETS: [(200, targets)],
            CLAIM: list(claims)}


def login_and_wait(home):
    run(home, "login")
    run(home, "login-wait")


def test_login_with_one_target_claims_it_and_keeps_the_key(
        tmp_path, monkeypatch, env, capsys):
    home = str(tmp_path)
    store_anonymous(home)
    fake = serve(monkeypatch, login_routes(
        [TARGET_A], (200, {"workspace_id": "w-a", "workspace_name": "Default",
                           "org_name": "Kiran's org"})))
    login_and_wait(home)

    claim_call = [c for c in fake.calls if c["key"] == CLAIM][0]
    assert claim_call["body"] == {"claim_token": TOKEN, "workspace_id": "w-a"}
    assert claim_call["auth"] == "Bearer auth0-tok"
    targets_call = [c for c in fake.calls if c["key"] == TARGETS][0]
    assert targets_call["auth"] == "Bearer auth0-tok"
    assert fake.count("POST", "/v1/device/exchange") == 0
    creds = json.load(open(login.credentials_path(home)))
    assert creds["anonymous"] is False
    assert creds["api_key"] == "ri_anonkey.secretpart"
    assert creds["workspace_id"] == "w-a"
    assert creds["org_name"] == "Kiran's org"
    assert "claim_token" not in creds
    assert mode(login.credentials_path(home)) == 0o600
    assert not os.path.exists(login.pending_path(home))
    out = capsys.readouterr().out
    assert "Claimed" in out and TOKEN not in out.split("Claimed", 1)[1]


def test_login_with_several_targets_lists_them_and_parks_the_token(
        tmp_path, monkeypatch, env, capsys):
    home = str(tmp_path)
    store_anonymous(home)
    fake = serve(monkeypatch, login_routes([TARGET_A, TARGET_B]))
    login_and_wait(home)

    assert fake.count(*CLAIM) == 0
    path = anonymous.claim_pending_path(home)
    assert mode(path) == 0o600
    pending = json.load(open(path))
    assert pending["access_token"] == "auth0-tok"
    assert pending["targets"] == [TARGET_A, TARGET_B]
    assert pending["expires_at"] > 0
    out = capsys.readouterr().out
    assert "1. Default (Kiran's org, admin)" in out
    assert "2. Team (GlassFlow, member)" in out
    assert "/rius claim" in out
    assert json.load(open(login.credentials_path(home)))["anonymous"] is True


def parked(home, monkeypatch, *claims):
    store_anonymous(home)
    anonymous.write_claim_pending(home, {"access_token": "auth0-tok",
                                         "expires_in": 3600},
                                  [TARGET_A, TARGET_B])
    return serve(monkeypatch, {CLAIM: list(claims)})


CLAIMED_B = (200, {"workspace_id": "w-b", "workspace_name": "Team",
                   "org_name": "GlassFlow"})


@pytest.mark.parametrize("choice", [["2"], ["Team"], ["w-b"]])
def test_claim_by_number_name_or_id_finishes_and_deletes_pending(
        tmp_path, monkeypatch, env, capsys, choice):
    home = str(tmp_path)
    fake = parked(home, monkeypatch, CLAIMED_B)
    run(home, "claim", *choice)
    assert fake.calls[0]["body"] == {"claim_token": TOKEN, "workspace_id": "w-b"}
    creds = json.load(open(login.credentials_path(home)))
    assert creds["anonymous"] is False
    assert creds["workspace_name"] == "Team"
    assert creds["api_key"] == "ri_anonkey.secretpart"
    assert not os.path.exists(anonymous.claim_pending_path(home))


def test_claim_with_an_unknown_choice_keeps_pending(tmp_path, monkeypatch, env, capsys):
    home = str(tmp_path)
    fake = parked(home, monkeypatch)
    run(home, "claim", "9")
    assert fake.calls == []
    assert os.path.exists(anonymous.claim_pending_path(home))
    assert "Team" in capsys.readouterr().out


def test_claim_with_an_expired_sign_in_restarts_login(
        tmp_path, monkeypatch, env, capsys):
    home = str(tmp_path)
    store_anonymous(home)
    anonymous.write_claim_pending(home, {"access_token": "auth0-tok",
                                         "expires_in": -1}, [TARGET_A, TARGET_B])
    fake = serve(monkeypatch, {DEVICE_CODE: [(200, DEVICE_CODE_RESPONSE)]})
    run(home, "claim", "2")
    assert fake.count(*CLAIM) == 0
    assert not os.path.exists(anonymous.claim_pending_path(home))
    assert "RIUS_LOGIN_PENDING:" in capsys.readouterr().out


def test_claim_without_pending_points_at_login(tmp_path, monkeypatch, env, capsys):
    home = str(tmp_path)
    store_anonymous(home)
    serve(monkeypatch, {})
    run(home, "claim", "1")
    assert "/rius login" in capsys.readouterr().out


@pytest.mark.parametrize("status, message", [
    (404, "invalid"), (409, "already claimed")])
def test_claim_refusals_are_explained(tmp_path, monkeypatch, env, capsys,
                                      status, message):
    home = str(tmp_path)
    parked(home, monkeypatch, (status, {"detail": "x"}))
    run(home, "claim", "2")
    assert message in capsys.readouterr().out.lower()


def test_claim_404_leaves_the_credential_unclaimed(tmp_path, monkeypatch, env):
    home = str(tmp_path)
    parked(home, monkeypatch, (404, {}))
    run(home, "claim", "2")
    creds = json.load(open(login.credentials_path(home)))
    assert creds["anonymous"] is True
    assert creds["claim_token"] == TOKEN


# --- RIUS_API_KEY wins: never claim over it -----------------------------------

def test_login_with_rius_api_key_never_claims(tmp_path, monkeypatch, env, capsys):
    home = str(tmp_path)
    store_anonymous(home)
    monkeypatch.setenv("RIUS_API_KEY", "ri_envkey")
    fake = serve(monkeypatch, login_routes([TARGET_A], CLAIMED_B))
    login_and_wait(home)
    assert fake.count(*TARGETS) == 0
    assert fake.count(*CLAIM) == 0
    assert json.load(open(login.credentials_path(home)))["anonymous"] is True
    assert "RIUS_API_KEY" in capsys.readouterr().out


def test_claim_with_rius_api_key_never_claims(tmp_path, monkeypatch, env, capsys):
    home = str(tmp_path)
    fake = parked(home, monkeypatch, CLAIMED_B)
    monkeypatch.setenv("RIUS_API_KEY", "ri_envkey")
    run(home, "claim", "2")
    assert fake.calls == []
    assert json.load(open(login.credentials_path(home)))["anonymous"] is True


def test_hooks_never_provision(tmp_path, monkeypatch, env):
    fake = serve(monkeypatch, {})
    c = config.resolve("s1", "/x", {"RIUS_CLAUDE_ENABLED": "true"}, str(tmp_path))
    assert c.enabled is False
    assert fake.calls == []


def test_claim_409_marks_the_credential_claimed_and_drops_the_token(
        tmp_path, monkeypatch, env):
    home = str(tmp_path)
    parked(home, monkeypatch, (409, {}))
    run(home, "claim", "2")
    creds = json.load(open(login.credentials_path(home)))
    assert creds["anonymous"] is False
    assert "claim_token" not in creds
    assert creds["api_key"] == "ri_anonkey.secretpart"
    assert not os.path.exists(anonymous.claim_pending_path(home))


def test_status_of_a_claimed_credential_names_its_workspace(
        tmp_path, monkeypatch, env, capsys):
    home = str(tmp_path)
    store_anonymous(home, anonymous=False, workspace_name="Team")
    run(home, "status", "--session", "s1", "--cwd", "/x")
    out = capsys.readouterr().out
    assert "Workspace: Team" in out
    assert "unclaimed" not in out


def test_claim_server_error_changes_nothing(tmp_path, monkeypatch, env, capsys):
    home = str(tmp_path)
    parked(home, monkeypatch, (500, {"detail": "boom"}))
    run(home, "claim", "2")
    assert "HTTP 500" in capsys.readouterr().out
    assert json.load(open(login.credentials_path(home)))["anonymous"] is True
    assert os.path.exists(anonymous.claim_pending_path(home))


@pytest.mark.parametrize("reply", [(401, {"title": "Unauthorized"}), (500, [])])
def test_targets_failure_claims_nothing(tmp_path, monkeypatch, env, capsys, reply):
    home = str(tmp_path)
    store_anonymous(home)
    routes = login_routes([])
    routes[TARGETS] = [reply]
    fake = serve(monkeypatch, routes)
    login_and_wait(home)
    assert fake.count(*CLAIM) == 0
    assert "HTTP %d" % reply[0] in capsys.readouterr().out
    assert json.load(open(login.credentials_path(home)))["anonymous"] is True
