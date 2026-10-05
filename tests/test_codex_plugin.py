"""The Codex packaging: its manifest, hooks and skills, and the Codex side of
hook.py and rius_ctl (hook trust, MCP sign-in, the login-wait hint)."""
import io
import json
import os
import pathlib
import re
import sys

import pytest

import hook as hook_mod
import rius_ctl
from rius_cc import agent, codex_trust, config, login, state

from tests import signed_in

ROOT = pathlib.Path(__file__).parent.parent
MANIFEST = ROOT / ".codex-plugin" / "plugin.json"
HOOKS = ROOT / "codex" / "hooks.json"
SKILLS = ROOT / "codex" / "skills"
PAYLOADS = pathlib.Path(__file__).parent / "fixtures" / "hook_payloads"
CODEX_EVENTS = {"SessionStart", "UserPromptSubmit", "PreToolUse",
                "PermissionRequest", "PostToolUse", "PreCompact",
                "PostCompact", "SubagentStart", "SubagentStop", "Stop"}
SKILL_NAMES = {"rius-login", "rius-logout", "rius-status", "rius-on",
               "rius-off", "rius-enable-here", "rius-enable-content-here",
               "rius-disable-here"}
CLAUDE_ONLY = ("CLAUDE_PLUGIN_ROOT", "hooks/hooks.json", "commands/",
               ".mcp.json", "headersHelper")


def _manifest():
    return json.loads(MANIFEST.read_text())


def _hook_commands():
    hooks = json.loads(HOOKS.read_text())["hooks"]
    return {event: [h["command"] for group in groups for h in group["hooks"]]
            for event, groups in hooks.items()}


def _skill(name):
    return (SKILLS / name / "SKILL.md").read_text()


# --- packaging ----------------------------------------------------------------

def test_the_manifest_points_at_codex_files_only():
    manifest = _manifest()
    assert manifest["name"] == "rius"
    assert (ROOT / manifest["hooks"]).is_file()
    assert (ROOT / manifest["skills"]).is_dir()
    text = MANIFEST.read_text() + HOOKS.read_text()
    assert not [s for s in CLAUDE_ONLY if s in text]


def test_every_manifest_carries_the_same_version():
    claude = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
    market = json.loads((ROOT / ".claude-plugin" / "marketplace.json")
                        .read_text())
    pyproject = re.search(r'^version = "([^"]+)"',
                          (ROOT / "pyproject.toml").read_text(), re.M).group(1)
    versions = {_manifest()["version"], claude["version"],
                market["plugins"][0]["version"], pyproject}
    assert versions == {"0.5.0"}


def test_the_mcp_server_signs_in_with_oauth():
    """With `bearer_token_env_var` set, Codex 0.144.1 uses bearer mode only:
    without RIUS_API_KEY it drops the server and ignores an OAuth login."""
    server = _manifest()["mcpServers"]["rius"]
    claude = json.loads((ROOT / ".mcp.json").read_text())["mcpServers"]["rius"]
    assert server["url"] == claude["url"]
    assert set(server) == {"type", "url"}


def test_every_hook_runs_hook_sh_for_codex_with_its_own_event():
    commands = _hook_commands()
    assert set(commands) <= CODEX_EVENTS
    assert {"SessionStart", "UserPromptSubmit", "PostToolUse", "Stop",
            "SubagentStop"} <= set(commands)
    for event, (command,) in commands.items():
        assert command == ('bash "${PLUGIN_ROOT}/scripts/hook.sh" '
                           "--agent codex %s" % event)


def test_the_skills_are_the_claude_code_commands():
    assert {p.name for p in SKILLS.iterdir()} == SKILL_NAMES
    commands = {p.stem for p in (ROOT / "commands").glob("*.md")}
    assert {name[len("rius-"):] for name in SKILL_NAMES} == commands


ACTIONS = {"rius-enable-content-here": "content-on-here"}


@pytest.mark.parametrize("name", sorted(SKILL_NAMES))
def test_a_skill_names_itself_and_runs_rius_ctl_for_codex(name):
    text = _skill(name)
    front = text.split("---")[1]
    assert re.search(r"^name: %s$" % name, front, re.M)
    assert re.search(r"^description: \S", front, re.M)
    action = ACTIONS.get(name, name[len("rius-"):])
    command = ('bash "<plugin root>/scripts/rius_ctl.sh" %s' % action)
    assert command in text and "--agent codex" in text
    assert "<plugin root>/codex/skills/%s/SKILL.md" % name in text


@pytest.mark.parametrize("name", ["rius-on", "rius-off", "rius-status"])
def test_session_skills_pass_codex_thread_id(name):
    assert '--session "${CODEX_THREAD_ID:-}"' in _skill(name)


@pytest.mark.parametrize("name", sorted(SKILL_NAMES - {"rius-status"}))
def test_skills_that_write_ask_for_escalation(name):
    assert 'sandbox_permissions: "require_escalated"' in _skill(name)


def test_the_login_skill_shows_the_code_before_waiting():
    text = _skill("rius-login")
    assert text.index("`Code:`") < text.index("login-wait --agent codex")
    assert "Still waiting" in text


# --- hook trust ---------------------------------------------------------------

TRUSTED = """\
model = "x"

[hooks.state."rius@rius-coding-agents:codex/hooks.json:stop:0:0"]
trusted_hash = "sha256:aa"

[hooks.state."rius@rius-coding-agents:codex/hooks.json:session_start:0:0"]
trusted_hash = "sha256:bb"

[hooks.state."other@m:codex/hooks.json:post_tool_use:0:0"]
trusted_hash = "sha256:cc"

[hooks.state."rius@rius-coding-agents:hooks/hooks.json:post_tool_use:0:0"]
trusted_hash = "sha256:dd"

[hooks.state."rius@rius-coding-agents:codex/hooks.json:user_prompt_submit:0:0"]
enabled = false
"""


def test_only_this_plugins_trusted_codex_hooks_count(tmp_path):
    config_toml = tmp_path / "config.toml"
    config_toml.write_text(TRUSTED)
    assert codex_trust.approved_events(str(config_toml)) == {"stop",
                                                             "session_start"}


def test_the_expected_events_are_the_hooks_file_in_snake_case():
    assert codex_trust.expected_events(str(ROOT)) == [
        "post_tool_use", "session_start", "stop", "subagent_stop",
        "user_prompt_submit"]


def test_status_line_says_how_many_hooks_are_approved(tmp_path):
    (tmp_path / "config.toml").write_text(TRUSTED)
    line = codex_trust.status_line(str(ROOT), str(tmp_path))
    assert line.startswith("Hooks: 2 of 5 approved.") and "/hooks" in line
    keys = "".join(
        '[hooks.state."rius@m:codex/hooks.json:%s:0:0"]\n'
        'trusted_hash = "x"\n' % e
        for e in codex_trust.expected_events(str(ROOT)))
    (tmp_path / "config.toml").write_text(keys)
    assert codex_trust.status_line(str(ROOT), str(tmp_path)) == (
        "Hooks: approved in /hooks (5 of 5)")


def test_no_config_means_nothing_approved(tmp_path):
    assert codex_trust.status_line(str(ROOT), str(tmp_path)).startswith(
        "Hooks: 0 of 5")


# --- rius_ctl --agent codex ---------------------------------------------------

@pytest.fixture
def codex_env(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.delenv("RIUS_API_KEY", raising=False)
    return str(tmp_path / "home")


def test_status_reports_hook_trust_and_mcp_sign_in(codex_env, capsys):
    rius_ctl.dispatch(["status", "--agent", "codex", "--cwd", "/tmp"],
                      codex_env)
    out = capsys.readouterr().out
    assert "Hooks: 0 of 5 approved." in out
    assert rius_ctl.CODEX_QUERYING_TRACES in out
    assert "/mcp" not in out


def test_status_reads_hook_trust_from_codex_home_but_writes_nothing_there(
        codex_env, tmp_path, capsys):
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text("".join(
        '[hooks.state."rius@m:codex/hooks.json:%s:0:0"]\n'
        'trusted_hash = "x"\n' % e
        for e in codex_trust.expected_events(str(ROOT))))
    rius_ctl.dispatch(["enable-here", "--agent", "codex", "--cwd", "/tmp"],
                      codex_env)
    rius_ctl.dispatch(["status", "--agent", "codex", "--cwd", "/tmp"],
                      codex_env)
    assert "Hooks: approved in /hooks (5 of 5)" in capsys.readouterr().out
    assert sorted(os.listdir(str(codex_home))) == ["config.toml"]


def test_claude_code_status_is_unchanged(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("RIUS_API_KEY", raising=False)
    rius_ctl.dispatch(["status", "--cwd", "/tmp"], str(tmp_path))
    out = capsys.readouterr().out
    assert rius_ctl.QUERYING_TRACES in out
    assert "Hooks:" not in out


def test_the_login_skill_waits_with_the_agent_flag():
    assert ('bash "<plugin root>/scripts/rius_ctl.sh" login-wait --agent codex'
            in _skill("rius-login"))


def test_connected_points_codex_at_its_own_mcp_sign_in(codex_env, capsys,
                                                       monkeypatch):
    creds = {"email": "a@b.c", "workspace_name": "w", "workspace_id": "1"}
    monkeypatch.setattr(login, "wait", lambda home: creds)
    rius_ctl.dispatch(["login-wait", "--agent", "codex", "--cwd", "/opt/p"],
                      codex_env)
    out = capsys.readouterr().out
    assert rius_ctl.CODEX_QUERYING_TRACES in out
    assert "$rius:rius-enable-here" in out and "/mcp" not in out


# --- hook.py --agent codex ----------------------------------------------------

def _hook(monkeypatch, tmp_path, event, payload, ppid=4242):
    calls = []

    class FakePopen:
        def __init__(self, cmd, **kw):
            calls.append(list(cmd))

    env = {"HOME": str(tmp_path / "home"), "CODEX_HOME": str(tmp_path / "cx")}
    with agent.using(agent.CODEX):
        signed_in.sign_in(env["HOME"])
        config.write_path_rules(env["HOME"], {"enabled_paths": [payload["cwd"]]})
    monkeypatch.setattr(hook_mod.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(os, "environ", env)
    monkeypatch.setattr(os, "getppid", lambda: ppid)
    monkeypatch.setattr(sys, "argv", ["hook.py", "--agent", "codex", event])
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(agent, "_active", agent.CLAUDE_CODE)
    hook_mod.main()
    for cmd in calls:
        if cmd[3].endswith(".json") and os.path.exists(cmd[3]):
            os.remove(cmd[3])
    return calls


def _codex_payload(name):
    return json.loads((PAYLOADS / name).read_text())


def test_an_explicit_codex_hook_is_not_taken_for_a_foreign_one(
        monkeypatch, tmp_path):
    payload = _codex_payload("codex_stop.json")
    calls = _hook(monkeypatch, tmp_path, "Stop", payload)
    assert [c[2].endswith("exporter.py") for c in calls] == [True]


def test_codex_session_start_watches_the_codex_process(monkeypatch, tmp_path):
    payload = _codex_payload("codex_session_start.json")
    calls = _hook(monkeypatch, tmp_path, "SessionStart", payload)
    assert len(calls) == 2
    home = str(tmp_path / "home")
    with agent.using(agent.CODEX):
        assert state.load(payload["session_id"], home)["cc_pid"] == 4242
    heartbeat = next(c for c in calls if c[2].endswith("heartbeat.py"))
    assert heartbeat[6] == "4242"


def test_an_orphaned_codex_hook_watches_nothing(monkeypatch, tmp_path):
    payload = _codex_payload("codex_session_start.json")
    _hook(monkeypatch, tmp_path, "SessionStart", payload, ppid=1)
    with agent.using(agent.CODEX):
        st = state.load(payload["session_id"], str(tmp_path / "home"))
    assert st["cc_pid"] == 0


def test_an_ephemeral_codex_session_starts_nothing(monkeypatch, tmp_path):
    payload = dict(_codex_payload("codex_session_start.json"),
                   transcript_path=None)
    assert _hook(monkeypatch, tmp_path, "SessionStart", payload) == []
    assert not (tmp_path / "home" / ".codex" / "rius" / "state").exists()
    assert not (tmp_path / "cx").exists()
