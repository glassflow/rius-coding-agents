"""Login for agents other than Claude Code: they name themselves to the
control plane, survive one that does not know the field yet, and keep their
own credentials."""
import dataclasses
import os
import socket

import pytest

import rius_ctl
from rius_cc import agent, login
from tests.test_login import (LINK_BASE, LINK_RESPONSE, TOKEN_RESPONSE, Clock,
                              scripted)

HOST = socket.gethostname()[:64]


@pytest.fixture
def cursor():
    with agent.using(agent.select("cursor", {})):
        yield


def _start(home, post):
    return login.start(home, post=post, now=Clock().now)


def test_claude_code_still_sends_only_the_client_name(tmp_path):
    post = scripted((201, LINK_RESPONSE))
    _start(str(tmp_path), post)
    assert [payload for _, payload, _ in post.calls] == [{"client_name": HOST}]


def test_other_agents_name_themselves(cursor, tmp_path):
    post = scripted((201, LINK_RESPONSE))
    _start(str(tmp_path), post)
    assert post.calls == [(LINK_BASE + "/v1/agent-links",
                           {"client_name": HOST, "agent": "cursor"}, None)]


def test_a_control_plane_without_the_field_gets_the_old_request(cursor,
                                                                 tmp_path):
    post = scripted((422, {"detail": "unexpected property"}),
                    (201, LINK_RESPONSE))
    pending = _start(str(tmp_path), post)
    assert [payload for _, payload, _ in post.calls] == [
        {"client_name": HOST, "agent": "cursor"}, {"client_name": HOST}]
    assert pending["user_code"] == LINK_RESPONSE["user_code"]


def test_other_failures_are_not_retried(cursor, tmp_path):
    post = scripted((500, {"detail": "boom"}))
    with pytest.raises(login.LoginError):
        _start(str(tmp_path), post)
    assert len(post.calls) == 1


def test_the_key_lands_in_the_agent_home_only(cursor, tmp_path):
    home, clock = str(tmp_path), Clock()
    login.start(home, post=scripted((201, LINK_RESPONSE)), now=clock.now)
    login.wait(home, post=scripted((200, TOKEN_RESPONSE)), sleep=clock.sleep,
               now=clock.now)
    stored = os.path.join(home, ".cursor", "rius", "credentials.json")
    assert login.credentials_path(home) == stored and os.path.exists(stored)
    with agent.using(agent.CLAUDE_CODE):
        assert login.read_credentials(home) is None


def test_the_profile_budget_can_only_lower_the_ceiling():
    clock = Clock()
    pending = {"env": "production", "device_code": "d", "interval": 5,
               "expires_at": clock.now() + 10000}
    short = dataclasses.replace(agent.CODEX, login_wait_budget=30)
    post = scripted(*[(428, {})] * 50)
    with agent.using(short):
        assert login.poll_for_key(pending, post=post, sleep=clock.sleep,
                                  now=clock.now) is None
    assert clock.t - 1000.0 <= 30


def test_login_messages_name_the_agent_command(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(login, "post_json", scripted((410, {})))
    monkeypatch.setattr(rius_ctl, "_open_browser", lambda url: None)
    rius_ctl.dispatch(["login-wait", "--agent", "cursor"], str(tmp_path))
    out = capsys.readouterr().out
    assert "Run `/rius-login` first." in out


def test_the_pending_wait_command_keeps_the_agent(cursor, monkeypatch,
                                                  tmp_path, capsys):
    monkeypatch.setattr(login, "post_json", scripted((201, LINK_RESPONSE)))
    monkeypatch.setattr(rius_ctl, "_open_browser", lambda url: None)
    rius_ctl.dispatch(["login", "--agent", "cursor", "--cwd", "/w"],
                      str(tmp_path))
    pending = [line for line in capsys.readouterr().out.splitlines()
               if line.startswith("RIUS_LOGIN_PENDING:")]
    assert pending and pending[0].endswith(" login-wait --agent cursor --cwd /w")


def test_claude_code_pending_wait_command_is_unchanged(monkeypatch, tmp_path,
                                                       capsys):
    monkeypatch.setattr(login, "post_json", scripted((201, LINK_RESPONSE)))
    monkeypatch.setattr(rius_ctl, "_open_browser", lambda url: None)
    rius_ctl.dispatch(["login", "--cwd", "/w"], str(tmp_path))
    assert "rius_ctl.sh login-wait --cwd /w" in capsys.readouterr().out
