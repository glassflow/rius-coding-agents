import json
import os
import stat
import subprocess
import sys
import pathlib

import pytest

from rius_cc import config, login

CTL = str(pathlib.Path(__file__).parent.parent / "scripts" / "rius_ctl.py")

DEVICE_CODE_RESPONSE = {
    "device_code": "dev-secret", "user_code": "ABCD-EFGH",
    "verification_uri": "https://auth.example/activate",
    "verification_uri_complete": "https://auth.example/activate?user_code=ABCD-EFGH",
    "interval": 5, "expires_in": 900,
}
EXCHANGE_RESPONSE = {
    "key": "ri_supersecretkey", "id": "k1", "prefix": "ri_supe",
    "scopes": ["ingest", "read"], "expires_at": "2027-09-25T00:00:00Z",
    "created_at": "2026-09-25T00:00:00Z", "workspace_id": "w1",
    "workspace_name": "Default",
}


class Clock:
    def __init__(self):
        self.t = 1000.0
        self.sleeps = []

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += seconds


def scripted(*responses):
    """A `post` that answers with each (status, body) in turn."""
    queue = list(responses)
    calls = []

    def post(url, payload, *rest):
        calls.append((url, payload, rest))
        return queue.pop(0)
    post.calls = calls
    return post


def _start(home, clock):
    return login.start(home, post=scripted((200, DEVICE_CODE_RESPONSE)), now=clock.now)


def _wait(home, clock, token_responses, exchange=(200, EXCHANGE_RESPONSE)):
    return login.wait(home, post_token=scripted(*token_responses),
                      post_exchange=scripted(exchange),
                      sleep=clock.sleep, now=clock.now)


def test_start_parks_the_device_code_privately(tmp_path):
    home = str(tmp_path)
    pending = _start(home, Clock())
    assert pending["user_code"] == "ABCD-EFGH"
    mode = stat.S_IMODE(os.stat(login.pending_path(home)).st_mode)
    assert mode == 0o600


def test_start_reports_an_auth0_refusal(tmp_path):
    with pytest.raises(login.LoginError, match="unauthorized_client"):
        login.start(str(tmp_path), post=scripted((403, {"error": "unauthorized_client"})))


def test_success_stores_a_private_credential_and_clears_pending(tmp_path):
    home, clock = str(tmp_path), Clock()
    _start(home, clock)
    creds = _wait(home, clock, [(403, {"error": "authorization_pending"}),
                                (200, {"access_token": "tok"})])
    assert creds["workspace_name"] == "Default"
    assert creds["endpoint"] == login.ENVIRONMENTS["staging"]["ingest_endpoint"]
    assert stat.S_IMODE(os.stat(login.credentials_path(home)).st_mode) == 0o600
    assert not os.path.exists(login.pending_path(home))


def test_exchange_accepts_201_created(tmp_path):
    # The live route answers 201: it creates a key.
    home, clock = str(tmp_path), Clock()
    _start(home, clock)
    creds = _wait(home, clock, [(200, {"access_token": "tok"})],
                  exchange=(201, EXCHANGE_RESPONSE))
    assert creds["api_key"] == "ri_supersecretkey"


def test_exchange_sends_the_auth0_token_as_bearer(tmp_path):
    home, clock = str(tmp_path), Clock()
    _start(home, clock)
    exchange = scripted((200, EXCHANGE_RESPONSE))
    login.wait(home, post_token=scripted((200, {"access_token": "tok"})),
               post_exchange=exchange, sleep=clock.sleep, now=clock.now)
    url, payload, rest = exchange.calls[0]
    assert url == login.ENVIRONMENTS["staging"]["exchange_url"]
    assert rest == ("tok",)
    assert "scopes" not in payload  # the server fixes scopes, never the client


def test_slow_down_widens_the_interval_for_good(tmp_path):
    home, clock = str(tmp_path), Clock()
    _start(home, clock)
    _wait(home, clock, [(429, {"error": "slow_down"}),
                        (403, {"error": "authorization_pending"}),
                        (200, {"access_token": "tok"})])
    assert clock.sleeps == [5, 10, 10]


@pytest.mark.parametrize("error, message", [
    ("access_denied", "declined"),
    ("expired_token", "expired"),
])
def test_denied_or_expired_leaves_no_credential_and_no_pending(tmp_path, error, message):
    home, clock = str(tmp_path), Clock()
    _start(home, clock)
    with pytest.raises(login.LoginError, match=message):
        _wait(home, clock, [(403, {"error": error})])
    assert not os.path.exists(login.credentials_path(home))
    assert not os.path.exists(login.pending_path(home))


def test_failed_exchange_leaves_no_credential(tmp_path):
    home, clock = str(tmp_path), Clock()
    _start(home, clock)
    with pytest.raises(login.LoginError, match="403"):
        _wait(home, clock, [(200, {"access_token": "tok"})],
              exchange=(403, {"detail": "not an admin"}))
    assert not os.path.exists(login.credentials_path(home))


def test_budget_exhausted_keeps_pending_for_another_wait(tmp_path):
    home, clock = str(tmp_path), Clock()
    _start(home, clock)
    pending = [(403, {"error": "authorization_pending"})] * 200
    assert _wait(home, clock, pending) is None
    assert clock.t - 1000.0 <= login.WAIT_BUDGET_SECONDS + 5
    assert os.path.exists(login.pending_path(home))
    assert not os.path.exists(login.credentials_path(home))


def test_code_expiring_mid_wait_is_terminal(tmp_path):
    home, clock = str(tmp_path), Clock()
    _start(home, clock)
    clock.t += 890  # the code has ten seconds left
    with pytest.raises(login.LoginError, match="expired"):
        _wait(home, clock, [(403, {"error": "authorization_pending"})] * 10)
    assert not os.path.exists(login.pending_path(home))


def test_wait_without_start_says_so(tmp_path):
    with pytest.raises(login.LoginError, match="/rius login"):
        _wait(str(tmp_path), Clock(), [])


# --- config picks the stored credential up ---------------------------------

def _store(home, **overrides):
    creds = {"api_key": "ri_stored", "endpoint": "https://ingest.stored",
             "workspace_name": "Default"}
    creds.update(overrides)
    login._write_private(login.credentials_path(home), creds)


def test_stored_credential_is_used_with_its_endpoint(tmp_path):
    home = str(tmp_path)
    _store(home)
    c = config.resolve("s1", "/x", {"RIUS_CLAUDE_ENABLED": "true"}, home)
    assert c.enabled is True
    assert c.api_key == "ri_stored"
    assert c.endpoint == "https://ingest.stored"
    assert c.key_source == "/rius login"


def test_env_key_beats_the_stored_credential_and_its_endpoint(tmp_path):
    home = str(tmp_path)
    _store(home)
    c = config.resolve("s1", "/x", {"RIUS_API_KEY": "ri_env"}, home)
    assert c.api_key == "ri_env"
    assert c.endpoint == config.DEFAULT_ENDPOINT
    assert c.key_source == "RIUS_API_KEY"


def test_corrupt_credentials_file_is_ignored(tmp_path):
    home = str(tmp_path)
    os.makedirs(os.path.dirname(login.credentials_path(home)))
    with open(login.credentials_path(home), "w") as fh:
        fh.write("{not json")
    c = config.resolve("s1", "/x", {"RIUS_CLAUDE_ENABLED": "true"}, home)
    assert c.enabled is False
    assert "/rius login" in c.reason


# --- the CLI ------------------------------------------------------------------

def _ctl(args, home, env=None):
    e = {"HOME": home, "PATH": "/usr/bin:/bin"}
    e.update(env or {})
    return subprocess.run([sys.executable, CTL] + args, capture_output=True,
                          text=True, env=e, timeout=30)


def test_status_names_the_key_source_and_workspace_but_not_the_key(tmp_path):
    home = str(tmp_path)
    _store(home, api_key="ri_supersecretkey")
    r = _ctl(["status", "--session", "s1", "--cwd", "/x"], home)
    assert "Key from: /rius login" in r.stdout
    assert "Workspace: Default" in r.stdout
    assert "supersecretkey" not in r.stdout


def test_status_without_any_key_points_at_login(tmp_path):
    r = _ctl(["status", "--session", "s1", "--cwd", "/x"], str(tmp_path))
    assert "/rius login" in r.stdout


def test_logout_removes_the_stored_credential(tmp_path):
    home = str(tmp_path)
    _store(home)
    r = _ctl(["logout"], home)
    assert "Removed" in r.stdout
    assert not os.path.exists(login.credentials_path(home))


def test_login_wait_without_login_fails_politely(tmp_path):
    r = _ctl(["login-wait"], str(tmp_path))
    assert r.returncode == 0
    assert "no sign-in in progress" in r.stdout
