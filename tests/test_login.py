import json
import os
import pathlib
import socket
import stat
import subprocess
import sys
import threading
import urllib.error

import pytest
from time import sleep as time_sleep

import rius_ctl
from rius_cc import config, login, platform_compat

CTL = str(pathlib.Path(__file__).parent.parent / "scripts" / "rius_ctl.py")
LINK_BASE = login.ENVIRONMENTS["staging"]["link_base"]
DEVICE_CODE = "dc_Zm9vYmFyYmF6cXV4cXV1eHF1dXhxdXV4cXV1eHF1dXg"

LINK_RESPONSE = {
    "link_id": "11111111-1111-1111-1111-111111111111",
    "device_code": DEVICE_CODE, "user_code": "ABCD-EFGH",
    "connect_url": "https://staging.rius.glassflow.xyz/portal/pick?link=1111",
    "interval": 5, "expires_in": 900,
}
TOKEN_RESPONSE = {
    "api_key": "ri_supersecretkey", "endpoint": "https://ingest.eu.staging",
    "mcp_url": "https://mcp.eu.staging/mcp",
    "workspace_id": "22222222-2222-2222-2222-222222222222",
    "workspace_name": "eng-shared", "org_name": "Acme",
    "email": "x@acme.com", "expires_at": "2026-12-26T00:00:00Z",
}
PENDING = (428, {"status": "pending"})


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
    """A `post` that answers with each (status, body) in turn, or raises it."""
    queue = list(responses)
    calls = []

    def post(url, payload, bearer=None):
        calls.append((url, payload, bearer))
        answer = queue.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer
    post.calls = calls
    return post


def _start(home, clock):
    return login.start(home, post=scripted((201, LINK_RESPONSE)), now=clock.now)


def _wait(home, clock, post):
    return login.wait(home, post=post, sleep=clock.sleep, now=clock.now)


def _mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def _store(home, **overrides):
    creds = {"api_key": "ri_stored", "endpoint": "https://ingest.stored",
             "env": "staging", "workspace_id": "33333333-3333-3333-3333-333333333333",
             "workspace_name": "personal", "org_name": "Me",
             "email": "x@acme.com", "expires_at": "2026-12-01T00:00:00Z"}
    creds.update(overrides)
    login._write_private(login.credentials_path(home), creds)


# --- start --------------------------------------------------------------------

def test_start_asks_for_a_link_named_after_this_machine(tmp_path):
    post = scripted((201, LINK_RESPONSE))
    login.start(str(tmp_path), post=post, now=Clock().now)
    url, payload, bearer = post.calls[0]
    assert url == LINK_BASE + "/v1/agent-links"
    assert payload == {"client_name": socket.gethostname()[:64]}
    assert bearer is None


def test_start_parks_the_link_privately(tmp_path):
    home = str(tmp_path)
    pending = _start(home, Clock())
    assert pending["user_code"] == "ABCD-EFGH"
    assert pending["expires_at"] == 1900.0
    stored = json.load(open(login.pending_path(home)))
    assert set(stored) == {"env", "link_id", "device_code", "user_code",
                           "connect_url", "interval", "expires_at"}
    assert _mode(login.pending_path(home)) == 0o600


def test_start_reports_an_unconfigured_environment(tmp_path):
    with pytest.raises(login.LoginError, match="agent_links_unconfigured"):
        login.start(str(tmp_path), post=scripted(
            (503, {"code": "agent_links_unconfigured"})))
    assert not os.path.exists(login.pending_path(str(tmp_path)))


@pytest.mark.parametrize("field", ["link_id", "device_code", "user_code", "connect_url",
                                   "interval", "expires_in"])
def test_start_requires_every_contract_field(tmp_path, field):
    body = dict(LINK_RESPONSE)
    del body[field]
    with pytest.raises(login.LoginError, match=field):
        login.start(str(tmp_path), post=scripted((201, body)))
    assert not os.path.exists(login.pending_path(str(tmp_path)))


def test_start_reports_an_unreachable_server(tmp_path):
    with pytest.raises(login.LoginError, match="reach"):
        login.start(str(tmp_path), post=scripted(urllib.error.URLError("dns")))


# --- wait -----------------------------------------------------------------------

def test_pending_then_success_stores_private_credentials(tmp_path):
    home, clock = str(tmp_path), Clock()
    _start(home, clock)
    post = scripted(PENDING, (200, TOKEN_RESPONSE))
    creds = _wait(home, clock, post)
    assert creds == {
        "api_key": "ri_supersecretkey", "endpoint": "https://ingest.eu.staging",
        "mcp_url": "https://mcp.eu.staging/mcp", "env": "staging",
        "workspace_id": "22222222-2222-2222-2222-222222222222",
        "workspace_name": "eng-shared", "org_name": "Acme",
        "email": "x@acme.com", "expires_at": "2026-12-26T00:00:00Z"}
    assert login.read_credentials(home) == creds
    assert _mode(login.credentials_path(home)) == 0o600
    assert not os.path.exists(login.pending_path(home))
    assert post.calls[0][:2] == (LINK_BASE + "/v1/agent-links/token",
                                 {"device_code": DEVICE_CODE})
    assert clock.sleeps == [5, 5]


@pytest.mark.parametrize("status, code", [(404, "link_not_found"),
                                          (410, "link_expired")])
def test_a_dead_link_is_terminal(tmp_path, status, code):
    home, clock = str(tmp_path), Clock()
    _start(home, clock)
    with pytest.raises(login.LoginError) as exc:
        _wait(home, clock, scripted((status, {"code": code})))
    assert str(exc.value) == "That sign-in link expired. Run `/rius:login` again."
    assert not os.path.exists(login.pending_path(home))
    assert not os.path.exists(login.credentials_path(home))


def test_network_blips_and_5xx_back_off_and_keep_polling(tmp_path):
    home, clock = str(tmp_path), Clock()
    _start(home, clock)
    post = scripted(urllib.error.URLError("reset"), (503, {"code": "mint_failed"}),
                    PENDING, (200, TOKEN_RESPONSE))
    creds = _wait(home, clock, post)
    assert creds["api_key"] == "ri_supersecretkey"
    assert clock.sleeps == [5, 10, 20, 5]


def test_a_socket_timeout_is_a_blip_too(tmp_path):
    home, clock = str(tmp_path), Clock()
    _start(home, clock)
    creds = _wait(home, clock, scripted(socket.timeout("slow"),
                                        (200, TOKEN_RESPONSE)))
    assert creds["email"] == "x@acme.com"


@pytest.mark.parametrize("field", ["endpoint", "workspace_id", "workspace_name",
                                   "email"])
def test_a_key_without_its_destination_is_refused(tmp_path, field):
    home, clock = str(tmp_path), Clock()
    _start(home, clock)
    body = dict(TOKEN_RESPONSE)
    del body[field]
    with pytest.raises(login.LoginError, match=field):
        _wait(home, clock, scripted((200, body)))
    assert not os.path.exists(login.credentials_path(home))


def test_an_unexpected_rejection_is_terminal(tmp_path):
    home, clock = str(tmp_path), Clock()
    _start(home, clock)
    with pytest.raises(login.LoginError, match="400"):
        _wait(home, clock, scripted((400, {"detail": "bad device_code"})))
    assert not os.path.exists(login.pending_path(home))


def test_budget_exhaustion_keeps_the_pending_link_and_a_resume_succeeds(tmp_path):
    home, clock = str(tmp_path), Clock()
    _start(home, clock)
    assert _wait(home, clock, scripted(*[PENDING] * 200)) is None
    assert clock.t - 1000.0 <= login.WAIT_BUDGET_SECONDS + 5
    assert os.path.exists(login.pending_path(home))
    assert not os.path.exists(login.credentials_path(home))

    creds = _wait(home, clock, scripted((200, TOKEN_RESPONSE)))
    assert creds["workspace_name"] == "eng-shared"
    assert not os.path.exists(login.pending_path(home))


class SlowNetwork:
    """Every request fails after using up the whole request timeout."""

    def __init__(self, clock, cost=login.REQUEST_TIMEOUT_SECONDS):
        self.clock, self.cost, self.calls = clock, cost, 0

    def __call__(self, url, payload, bearer=None):
        self.calls += 1
        self.clock.t += self.cost
        raise urllib.error.URLError("unreachable")


def test_sustained_network_failure_stays_inside_the_budget(tmp_path):
    home, clock = str(tmp_path), Clock()
    _start(home, clock)
    network = SlowNetwork(clock)
    assert _wait(home, clock, network) is None
    elapsed = clock.t - 1000.0
    assert elapsed <= login.WAIT_BUDGET_SECONDS + login.REQUEST_TIMEOUT_SECONDS
    assert network.calls > 1
    assert os.path.exists(login.pending_path(home))


def test_the_link_expiring_mid_wait_is_terminal(tmp_path):
    home, clock = str(tmp_path), Clock()
    _start(home, clock)
    clock.t += 890
    with pytest.raises(login.LoginError, match="expired"):
        _wait(home, clock, scripted(*[PENDING] * 10))
    assert not os.path.exists(login.pending_path(home))


def test_wait_without_start_says_so(tmp_path):
    with pytest.raises(login.LoginError, match="/rius:login"):
        _wait(str(tmp_path), Clock(), scripted())


def test_a_leftover_pending_file_of_another_shape_is_ignored(tmp_path):
    home = str(tmp_path)
    login._write_private(login.pending_path(home), {"verification_uri": "x"})
    with pytest.raises(login.LoginError, match="no sign-in in progress"):
        _wait(home, Clock(), scripted())


# --- private files -----------------------------------------------------------

def test_private_writes_leave_no_temp_files_and_ignore_a_stale_one(tmp_path):
    path = login.credentials_path(str(tmp_path))
    os.makedirs(os.path.dirname(path))
    open(path + ".tmp", "w").close()
    login._write_private(path, {"api_key": "a"})
    login._write_private(path, {"api_key": "b"})
    assert json.load(open(path)) == {"api_key": "b"}
    assert _mode(path) == 0o600
    assert sorted(os.listdir(os.path.dirname(path))) == [
        "credentials.json", "credentials.json.tmp"]


# --- one wait at a time -------------------------------------------------------

def _hold_wait_lock(home):
    path = login.wait_lock_path(home)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = platform_compat.open_lock_file(path)
    assert platform_compat.try_lock(fd)
    return fd


def test_a_second_wait_does_not_poll_while_another_holds_the_link(tmp_path):
    home, clock = str(tmp_path), Clock()
    _start(home, clock)
    fd = _hold_wait_lock(home)
    try:
        post = scripted()
        began = clock.t
        with pytest.raises(login.WaitInProgress):
            _wait(home, clock, post)
        assert post.calls == []
        assert (login.LOCK_WAIT_SECONDS <= clock.t - began
                <= login.LOCK_WAIT_SECONDS + login.LOCK_RETRY_SECONDS)
        assert os.path.exists(login.pending_path(home))
    finally:
        os.close(fd)


def test_the_wait_lock_is_private_and_released_afterwards(tmp_path):
    home, clock = str(tmp_path), Clock()
    _start(home, clock)
    _wait(home, clock, scripted((200, TOKEN_RESPONSE)))
    assert _mode(login.wait_lock_path(home)) == 0o600
    os.close(_hold_wait_lock(home))


def _restart_then(home, clock, answer):
    """A poll during which the user runs /rius:login again."""
    def post(url, payload, bearer=None):
        login.start(home, post=scripted((201, dict(LINK_RESPONSE, link_id="l-new"))),
                    now=clock.now)
        return answer
    return post


def test_an_older_wait_leaves_a_newer_login_s_link_alone(tmp_path):
    home, clock = str(tmp_path), Clock()
    _start(home, clock)
    _wait(home, clock, _restart_then(home, clock, (200, TOKEN_RESPONSE)))
    assert login.load_pending(home)["link_id"] == "l-new"


def test_a_superseded_wait_stops_within_one_interval(tmp_path):
    home, clock = str(tmp_path), Clock()
    _start(home, clock)
    restarted_at = []

    def post(url, payload, bearer=None):
        post.calls += 1
        if not restarted_at:
            _restart_then(home, clock, None)(url, payload)
            restarted_at.append(clock.t)
        return PENDING
    post.calls = 0
    with pytest.raises(login.Superseded):
        _wait(home, clock, post)
    assert post.calls == 1
    assert clock.t - restarted_at[0] <= LINK_RESPONSE["interval"]
    assert login.load_pending(home)["link_id"] == "l-new"
    assert not os.path.exists(login.credentials_path(home))
    os.close(_hold_wait_lock(home))


def test_a_newer_wait_takes_over_once_the_older_one_is_superseded(tmp_path):
    home = str(tmp_path)
    _park(home, link_id="l-old")
    restarted, b_retrying = threading.Event(), threading.Event()
    outcome = {}

    def old_post(url, payload, bearer=None):
        restarted.wait(5)
        old_post.calls += 1
        return PENDING if old_post.calls < 50 else (410, {})
    old_post.calls = 0

    def old_sleep(seconds):
        if restarted.is_set():
            b_retrying.wait(5)

    def run_old():
        try:
            login.wait(home, post=old_post, sleep=old_sleep)
        except BaseException as exc:
            outcome["old"] = exc

    def new_sleep(seconds):
        b_retrying.set()
        time_sleep(0.01)

    old = threading.Thread(target=run_old, daemon=True)
    old.start()
    try:
        while not os.path.exists(login.wait_lock_path(home)):
            time_sleep(0.01)
        _park(home, link_id="l-new", device_code="dc_new")
        restarted.set()
        new_post = scripted((200, TOKEN_RESPONSE))
        creds = login.wait(home, post=new_post, sleep=new_sleep)
    finally:
        restarted.set()
        b_retrying.set()
        old.join(10)
    assert isinstance(outcome.get("old"), login.Superseded)
    assert creds["api_key"] == "ri_supersecretkey"
    assert new_post.calls[0][1] == {"device_code": "dc_new"}
    assert not os.path.exists(login.pending_path(home))


def test_an_older_wait_failing_leaves_a_newer_login_s_link_alone(tmp_path):
    home, clock = str(tmp_path), Clock()
    _start(home, clock)
    with pytest.raises(login.LoginError):
        _wait(home, clock, _restart_then(home, clock, (410, {})))
    assert login.load_pending(home)["link_id"] == "l-new"


# --- the previous key -----------------------------------------------------------

def test_a_new_login_revokes_the_previous_key(tmp_path):
    home, clock = str(tmp_path), Clock()
    _store(home)
    _start(home, clock)
    post = scripted((200, TOKEN_RESPONSE), (204, {}))
    _wait(home, clock, post)
    assert post.calls[1] == (
        LINK_BASE + "/v1/agent-keys/revoke",
        {"workspace_id": "33333333-3333-3333-3333-333333333333"}, "ri_stored")
    assert login.read_credentials(home)["api_key"] == "ri_supersecretkey"


@pytest.mark.parametrize("failure", [(401, {}), (503, {}),
                                     urllib.error.URLError("down")])
def test_a_failed_revoke_does_not_fail_the_login(tmp_path, failure):
    home, clock = str(tmp_path), Clock()
    _store(home)
    _start(home, clock)
    creds = _wait(home, clock, scripted((200, TOKEN_RESPONSE), failure))
    assert creds["api_key"] == "ri_supersecretkey"
    assert login.read_credentials(home)["api_key"] == "ri_supersecretkey"


def test_an_already_revoked_key_counts_as_revoked(tmp_path):
    assert login.revoke({"api_key": "ri_gone", "env": "staging"},
                        post=scripted((401, {}))) is True


def test_the_same_key_coming_back_is_not_revoked(tmp_path):
    home, clock = str(tmp_path), Clock()
    _store(home, api_key="ri_supersecretkey")
    _start(home, clock)
    post = scripted((200, TOKEN_RESPONSE))
    _wait(home, clock, post)
    assert len(post.calls) == 1


# --- config picks the stored credential up ---------------------------------

def test_stored_credential_is_used_with_its_endpoint(tmp_path):
    home = str(tmp_path)
    _store(home)
    c = config.resolve("s1", "/x", {"RIUS_CLAUDE_ENABLED": "true"}, home)
    assert c.enabled is True
    assert c.api_key == "ri_stored"
    assert c.endpoint == "https://ingest.stored"
    assert c.key_source == "/rius:login"


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
    assert "/rius:login" in c.reason


# --- the CLI, over the real JSON transport -----------------------------------

class FakeServer:
    """Stands in for `login._send`, so the CLI's real request building runs."""

    def __init__(self, *responses):
        self.post = scripted(*responses)

    def __call__(self, req, timeout=15.0):
        bearer = req.headers.get("Authorization")
        return self.post(req.full_url, json.loads(req.data.decode("utf-8")),
                         bearer[len("Bearer "):] if bearer else None)

    @property
    def calls(self):
        return self.post.calls


@pytest.fixture
def server(monkeypatch):
    def install(*responses):
        fake = FakeServer(*responses)
        monkeypatch.setattr(login, "_send", fake)
        monkeypatch.setattr(rius_ctl, "_open_browser", lambda url: None)
        return fake
    monkeypatch.delenv("RIUS_API_KEY", raising=False)
    return install


def _park(home, **overrides):
    pending = {"env": "staging", "link_id": "l1", "device_code": DEVICE_CODE,
               "user_code": "ABCD-EFGH", "connect_url": "https://c/pick",
               "interval": 0, "expires_at": 4102444800}
    pending.update(overrides)
    login._write_private(login.pending_path(home), pending)


def _enable(home, *paths):
    login._write_private(config.path_rules_path(home),
                         {"enabled_paths": list(paths)})


def test_login_prints_the_disclosure_url_and_code_but_not_the_device_code(
        tmp_path, server, capsys):
    server((201, LINK_RESPONSE))
    rius_ctl.dispatch(["login", "--cwd", "/opt/proj"], str(tmp_path))
    out = capsys.readouterr().out
    assert login.DISCLOSURE in out
    assert login.DISCLOSURE == (
        "Folders you enable send full sessions (prompts, replies, file "
        "contents, command output) to the workspace you pick. Everyone with "
        "access to that workspace, including its admins, can read them.")
    assert "Open:  " + LINK_RESPONSE["connect_url"] in out
    assert "Code:  ABCD-EFGH" in out
    assert "RIUS_LOGIN_PENDING: bash " in out
    assert "login-wait --cwd /opt/proj" in out
    assert DEVICE_CODE not in out


def test_login_wait_success_says_where_it_landed(tmp_path, server, capsys):
    home = str(tmp_path)
    _park(home)
    server((200, TOKEN_RESPONSE))
    rius_ctl.dispatch(["login-wait", "--cwd", "/opt/proj"], home)
    out = capsys.readouterr().out
    assert out.splitlines()[:3] == [
        "Connected as x@acme.com → eng-shared (Acme).",
        "Trace this folder (/opt/proj)? Run /rius:enable-here.",
        'Reconnect "rius" in /mcp to query your traces.']
    assert "enabled folder" not in out
    assert "supersecretkey" not in out and DEVICE_CODE not in out


def test_login_wait_warns_when_enabled_folders_change_workspace(
        tmp_path, server, capsys):
    home = str(tmp_path)
    _store(home)
    _enable(home, "/a", "/b", "/c")
    _park(home)
    server((200, TOKEN_RESPONSE), (204, {}))
    rius_ctl.dispatch(["login-wait", "--cwd", "/opt/proj"], home)
    out = capsys.readouterr().out
    assert ("3 enabled folders will now send to eng-shared instead of "
            "personal.") in out


def test_login_wait_is_quiet_about_folders_when_the_workspace_is_the_same(
        tmp_path, server, capsys):
    home = str(tmp_path)
    _store(home, workspace_id=TOKEN_RESPONSE["workspace_id"])
    _enable(home, "/a")
    _park(home)
    server((200, TOKEN_RESPONSE), (204, {}))
    rius_ctl.dispatch(["login-wait", "--cwd", "/opt/proj"], home)
    assert "enabled folder" not in capsys.readouterr().out


def test_login_wait_without_an_org_prints_no_placeholder(tmp_path, server, capsys):
    home = str(tmp_path)
    _park(home)
    body = dict(TOKEN_RESPONSE)
    del body["org_name"]
    server((200, body))
    rius_ctl.dispatch(["login-wait", "--cwd", "/opt/proj"], home)
    out = capsys.readouterr().out
    assert out.splitlines()[0] == "Connected as x@acme.com → eng-shared."
    assert "None" not in out


def test_login_wait_under_a_dead_network_still_says_it_is_waiting(
        tmp_path, capsys, monkeypatch):
    home, clock = str(tmp_path), Clock()
    _park(home, interval=5, expires_at=clock.now() + 900)
    real_wait = login.wait
    monkeypatch.setattr(login, "wait", lambda h: real_wait(
        h, post=SlowNetwork(clock), sleep=clock.sleep, now=clock.now))
    rius_ctl.dispatch(["login-wait", "--cwd", "/opt/proj"], home)
    out = capsys.readouterr().out
    assert clock.t - 1000.0 <= login.WAIT_BUDGET_SECONDS + login.REQUEST_TIMEOUT_SECONDS
    assert "Still waiting" in out and "RIUS_LOGIN_PENDING: bash " in out
    assert os.path.exists(login.pending_path(home))


def test_login_wait_still_waiting_repeats_the_pending_line(
        tmp_path, server, capsys, monkeypatch):
    home = str(tmp_path)
    _park(home, expires_at=4102444800)
    monkeypatch.setattr(login, "WAIT_BUDGET_SECONDS", 0)
    server()
    rius_ctl.dispatch(["login-wait", "--cwd", "/opt/proj"], home)
    out = capsys.readouterr().out
    assert "Still waiting" in out
    assert "RIUS_LOGIN_PENDING: bash " in out and "login-wait --cwd /opt/proj" in out
    assert DEVICE_CODE not in out


def test_login_wait_while_another_waits_says_so_and_does_not_loop(
        tmp_path, server, capsys, monkeypatch):
    home = str(tmp_path)
    monkeypatch.setattr(login, "LOCK_WAIT_SECONDS", 0.0)
    _park(home)
    fake = server()
    fd = _hold_wait_lock(home)
    try:
        rius_ctl.dispatch(["login-wait", "--cwd", "/opt/proj"], home)
    finally:
        os.close(fd)
    out = capsys.readouterr().out
    assert "another login is already waiting" in out.lower()
    assert "Still waiting" not in out and "RIUS_LOGIN_PENDING" not in out
    assert fake.calls == []


def test_a_superseded_login_wait_says_so_and_does_not_loop(
        tmp_path, capsys, monkeypatch):
    home, clock = str(tmp_path), Clock()
    _park(home, interval=5)
    real_wait = login.wait
    monkeypatch.setattr(login, "wait", lambda h: real_wait(
        h, post=_restart_then(h, clock, PENDING), sleep=clock.sleep, now=clock.now))
    rius_ctl.dispatch(["login-wait", "--cwd", "/opt/proj"], home)
    out = capsys.readouterr().out
    assert "newer /rius:login" in out
    assert "Still waiting" not in out and "RIUS_LOGIN_PENDING" not in out
    assert "Rius login failed" not in out


def test_logout_revokes_then_deletes(tmp_path, server, capsys):
    home = str(tmp_path)
    _store(home)
    fake = server((204, {}))
    rius_ctl.dispatch(["logout"], home)
    assert capsys.readouterr().out.strip() == "Signed out; the key was revoked."
    assert fake.calls == [(LINK_BASE + "/v1/agent-keys/revoke",
                           {"workspace_id": "33333333-3333-3333-3333-333333333333"},
                           "ri_stored")]
    assert not os.path.exists(login.credentials_path(home))


def test_logout_of_an_already_revoked_key_is_a_clean_sign_out(tmp_path, server,
                                                             capsys):
    home = str(tmp_path)
    _store(home)
    server((401, {}))
    rius_ctl.dispatch(["logout"], home)
    assert capsys.readouterr().out.strip() == "Signed out; the key was revoked."
    assert not os.path.exists(login.credentials_path(home))


def test_relogin_over_an_already_revoked_key_says_nothing_about_it(
        tmp_path, server, capsys):
    home = str(tmp_path)
    _store(home)
    _park(home)
    server((200, TOKEN_RESPONSE), (401, {}))
    rius_ctl.dispatch(["login-wait", "--cwd", "/opt/proj"], home)
    assert "revoke" not in capsys.readouterr().out


@pytest.mark.parametrize("failure", [(503, {}), urllib.error.URLError("down")])
def test_logout_still_signs_out_when_the_revoke_fails(tmp_path, server, capsys,
                                                      failure):
    home = str(tmp_path)
    _store(home)
    server(failure)
    rius_ctl.dispatch(["logout"], home)
    assert capsys.readouterr().out.strip() == (
        "Signed out; could not revoke the key (it expires 2026-12-01).")
    assert not os.path.exists(login.credentials_path(home))


def test_logout_with_nothing_stored(tmp_path, server, capsys):
    fake = server()
    rius_ctl.dispatch(["logout"], str(tmp_path))
    assert "No stored Rius key" in capsys.readouterr().out
    assert fake.calls == []


def _ctl(args, home, env=None):
    e = {"HOME": home, "PATH": "/usr/bin:/bin"}
    e.update(env or {})
    return subprocess.run([sys.executable, CTL] + args, capture_output=True,
                          text=True, env=e, timeout=30)


def test_status_names_the_key_source_and_workspace_but_not_the_key(tmp_path):
    home = str(tmp_path)
    _store(home, api_key="ri_supersecretkey")
    r = _ctl(["status", "--session", "s1", "--cwd", "/x"], home)
    assert "Key from: /rius:login" in r.stdout
    assert "Workspace: personal" in r.stdout
    assert "supersecretkey" not in r.stdout


def test_status_without_any_key_points_at_login(tmp_path):
    r = _ctl(["status", "--session", "s1", "--cwd", "/x"], str(tmp_path))
    assert "/rius:login" in r.stdout


def test_login_wait_without_login_fails_politely(tmp_path):
    r = _ctl(["login-wait"], str(tmp_path))
    assert r.returncode == 0
    assert "no sign-in in progress" in r.stdout
