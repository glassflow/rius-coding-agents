"""The agent profile: Claude Code resolves to exactly the old literals, and
`--agent` moves every path and name the shared core uses to that agent."""
import io
import json
import os
import pathlib
import subprocess
import sys

import pytest

import exporter
import hook as hook_mod
import rius_ctl
from rius_cc import agent, config, log, login, spans, state
from tests.platforms import BASH, TOOLS_WITHOUT_PYTHON, posix_only
from tests.test_hook import _fakebin

HOOK_SH = str(pathlib.Path(__file__).parent.parent / "scripts" / "hook.sh")


@pytest.fixture
def codex(tmp_path):
    codex_home = tmp_path / "codex-home"
    with agent.using(agent.select("codex", {"CODEX_HOME": str(codex_home)})):
        yield codex_home


# --- profiles -----------------------------------------------------------------

def test_claude_code_is_the_default_and_keeps_its_literals(tmp_path):
    cc = agent.active()
    assert cc is agent.CLAUDE_CODE
    assert cc.rius_dir("/h") == os.path.join("/h", ".claude", "rius")
    assert (cc.service_name, cc.root_name, cc.provider) == (
        "claude-code", "claude-code session", "anthropic")
    assert (cc.attr_prefix, cc.env_var("DEBUG")) == ("cc.", "RIUS_CLAUDE_DEBUG")
    assert cc.command("login") == "/rius:login"
    assert config.DEFAULT_SERVICE_NAME == cc.service_name
    assert (spans.PROVIDER_NAME, spans.DEFAULT_ROOT_NAME) == (
        cc.provider, cc.root_name)


def test_codex_home_follows_codex_home_and_falls_back_to_dot_codex():
    bound = agent.select("codex", {"CODEX_HOME": "/elsewhere"})
    assert bound.rius_dir("/h") == os.path.join("/elsewhere", "rius")
    plain = agent.select("codex", {})
    assert plain.rius_dir("/h") == os.path.join("/h", ".codex", "rius")


def test_cursor_lives_under_dot_cursor():
    cursor = agent.select("cursor", {})
    assert cursor.state_dir("/h") == os.path.join("/h", ".cursor", "rius",
                                                  "state")
    assert cursor.command("login") == "/rius-login"


def test_unknown_agent_is_refused():
    with pytest.raises(agent.UnknownAgent):
        agent.select("vim", {})
    with pytest.raises(agent.UnknownAgent):
        agent.split_flag(["Stop", "--agent"])


def test_the_flag_is_taken_out_wherever_it_sits():
    assert agent.split_flag(["Stop"]) == ("claude-code", ["Stop"])
    assert agent.split_flag(["--agent", "codex", "Stop"]) == ("codex", ["Stop"])
    assert agent.split_flag(["p.json", "id", "--agent", "cursor"]) == (
        "cursor", ["p.json", "id"])


def test_children_of_claude_code_get_no_flag():
    assert agent.child_argv(agent.CLAUDE_CODE) == []
    assert agent.child_argv(agent.CODEX) == ["--agent", "codex"]


def test_using_restores_the_previous_profile():
    with agent.using(agent.CURSOR):
        assert agent.active() is agent.CURSOR
    assert agent.active() is agent.CLAUDE_CODE


def test_localize_rewords_commands_and_the_agent_name():
    text = "Run `/rius:login`, then restart Claude Code."
    assert agent.CLAUDE_CODE.localize(text) == text
    assert agent.CODEX.localize(text) == "Run `$rius:rius-login`, then restart Codex."
    assert agent.CURSOR.localize(text) == "Run `/rius-login`, then restart Cursor."


# --- the shared core under another agent ------------------------------------

def test_every_path_moves_to_the_agent_home(codex, tmp_path):
    home = str(tmp_path / "home")
    rius = str(codex / "rius")
    assert config.path_rules_path(home) == os.path.join(rius, "config.json")
    assert login.credentials_path(home) == os.path.join(rius, "credentials.json")
    assert state.state_dir(home) == os.path.join(rius, "state")
    assert log.log_dir(home) == os.path.join(rius, "log")
    assert not os.path.exists(os.path.join(home, ".claude"))


def test_config_uses_the_agent_env_names_and_service(codex, tmp_path):
    env = {"RIUS_API_KEY": "glassflow_k", "RIUS_CODEX_ENABLED": "true",
           "RIUS_CLAUDE_ENABLED": "false", "RIUS_CODEX_DEBUG": "1"}
    cfg = config.resolve("s1", "/tmp/p", env, str(tmp_path))
    assert cfg.enabled and cfg.reason == "on: RIUS_CODEX_ENABLED"
    assert cfg.service_name == "codex" and cfg.debug


def test_env_key_still_beats_the_agent_credentials_file(codex, tmp_path):
    home = str(tmp_path)
    login._write_private(login.credentials_path(home), {
        "api_key": "ri_stored", "endpoint": "https://stored", "env": "staging",
        "workspace_name": "w"})
    stored = config.resolve("s", "/tmp", {}, home)
    assert (stored.api_key, stored.key_source) == ("ri_stored", "/rius:login")
    env_key = config.resolve("s", "/tmp", {"RIUS_API_KEY": "glassflow_env"},
                             home)
    assert (env_key.api_key, env_key.key_source) == ("glassflow_env",
                                                     "RIUS_API_KEY")


def test_codex_resource_attributes_use_its_prefix(codex, tmp_path,
                                                  fixtures_dir, monkeypatch):
    sent = []
    monkeypatch.setattr(exporter.otlp, "encode",
                        lambda attrs, out: sent.append(attrs) or b"x")
    monkeypatch.setattr(exporter.otlp, "export", lambda *a, **k: 200)
    home = str(tmp_path / "home")
    config.write_path_rules(home, {"enabled_paths": ["/tmp"]})
    exporter.run("Stop", {"session_id": "s1", "cwd": "/tmp/proj",
                          "transcript_path": str(
                              fixtures_dir / "codex"
                              / "mock_tools_mcp_resume.jsonl")},
                 {"RIUS_API_KEY": "glassflow_k"}, home)
    assert sent and sent[0]["service.name"] == "codex"
    assert "codex.cwd" in sent[0] and "cc.cwd" not in sent[0]


# --- entry points -------------------------------------------------------------

def _hook_in_process(monkeypatch, argv, payload, env):
    calls = []

    class FakePopen:
        def __init__(self, cmd, **kw):
            if not any(str(a).endswith(("exporter.py", "heartbeat.py"))
                       for a in cmd):
                raise OSError("no ps in this test")
            calls.append(list(cmd))

    monkeypatch.setattr(hook_mod.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(os, "environ", dict(env))
    monkeypatch.setattr(sys, "argv", ["hook.py"] + argv)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(agent, "_active", agent.CLAUDE_CODE)
    hook_mod.main()
    for cmd in calls:
        if cmd[2].endswith(".json"):
            os.remove(cmd[2])
    return calls


def test_hook_hands_the_agent_to_both_children(monkeypatch, tmp_path):
    home, codex_home = tmp_path / "home", tmp_path / "codex"
    env = {"HOME": str(home), "CODEX_HOME": str(codex_home),
           "RIUS_API_KEY": "glassflow_k", "RIUS_CODEX_ENABLED": "true",
           "PATH": os.environ.get("PATH", "")}
    calls = _hook_in_process(
        monkeypatch, ["--agent", "codex", "SessionStart"],
        {"session_id": "s1", "cwd": str(tmp_path), "transcript_path": "/x"},
        env)
    assert len(calls) == 2
    assert all(cmd[-2:] == ["--agent", "codex"] for cmd in calls)
    assert (codex_home / "rius" / "state" / "s1.json").exists()
    assert not (home / ".claude").exists()


def test_hook_with_an_unknown_agent_does_nothing(monkeypatch, tmp_path):
    env = {"HOME": str(tmp_path), "RIUS_API_KEY": "glassflow_k",
           "RIUS_CLAUDE_ENABLED": "true"}
    calls = _hook_in_process(
        monkeypatch, ["--agent", "vim", "SessionStart"],
        {"session_id": "s1", "cwd": str(tmp_path), "transcript_path": "/x"},
        env)
    assert calls == []
    assert not (tmp_path / ".claude").exists()


def test_rius_ctl_speaks_the_agent_commands(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.delenv("RIUS_API_KEY", raising=False)
    home = str(tmp_path / "home")
    rius_ctl.dispatch(["status", "--agent", "codex", "--cwd", "/tmp"], home)
    out = capsys.readouterr().out
    assert "$rius:rius-login" in out and "/rius:" not in out
    assert agent.active() is agent.CLAUDE_CODE


def test_rius_ctl_writes_rules_in_the_agent_home(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    home = str(tmp_path / "home")
    rius_ctl.dispatch(["enable-here", "--agent", "codex", "--cwd",
                       str(tmp_path)], home)
    assert (tmp_path / "codex" / "rius" / "config.json").exists()
    assert not os.path.exists(os.path.join(home, ".claude"))


def test_rius_ctl_refuses_an_unknown_agent(tmp_path, capsys):
    rius_ctl.dispatch(["status", "--agent", "vim"], str(tmp_path))
    assert "unknown agent 'vim'" in capsys.readouterr().out


@posix_only("the fallback breadcrumb is a POSIX-shell path")
def test_launcher_breadcrumb_lands_in_the_agent_home(tmp_path):
    codex_home = tmp_path / "codex"
    env = {"HOME": str(tmp_path / "home"), "CODEX_HOME": str(codex_home),
           "PATH": (_fakebin(tmp_path, python3="fail", python="fail")
                    + os.pathsep + TOOLS_WITHOUT_PYTHON)}
    r = subprocess.run([BASH, HOOK_SH, "--agent", "codex", "Stop"],
                       input="{}", capture_output=True, text=True, env=env,
                       timeout=30)
    assert r.returncode == 0 and r.stdout == ""
    crumb = codex_home / "rius" / "log" / "bootstrap.log"
    assert 'rius hook "Stop"' in crumb.read_text()
