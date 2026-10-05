"""Cursor and Codex run Claude Code plugin hooks too. Their payloads must
leave no trace: no spawn, no state, nothing on stdout."""
import json
import os
import pathlib
import subprocess
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "scripts"))

from rius_cc import foreign_agent, state  # noqa: E402
from tests.test_hook import HOOK, _enabled_env, _run_in_process  # noqa: E402

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "hook_payloads"

FOREIGN = [
    ("SessionStart", "cursor_session_start.json"),
    ("PostToolUse", "cursor_post_tool_use.json"),
    ("SessionStart", "codex_session_start.json"),
    ("Stop", "codex_stop.json"),
]


def _payload(name, cwd):
    payload = json.loads((FIXTURES / name).read_text())
    payload["cwd"] = str(cwd)
    return payload


def _state_files(home):
    path = state.state_dir(home)
    return os.listdir(path) if os.path.isdir(path) else []


def _without_agent_env(env):
    return {k: v for k, v in env.items()
            if not k.startswith(("CURSOR_", "CODEX_"))}


@pytest.mark.parametrize("event,name", FOREIGN)
def test_a_foreign_payload_spawns_nothing_and_writes_no_state(
        tmp_path, monkeypatch, event, name):
    env, home = _enabled_env(tmp_path)
    calls = _run_in_process(monkeypatch, event, _payload(name, tmp_path),
                            _without_agent_env(env), home)
    assert calls == []
    assert _state_files(home) == []


@pytest.mark.parametrize("event,name", FOREIGN)
def test_a_foreign_payload_exits_zero_and_silent(tmp_path, event, name):
    env, home = _enabled_env(tmp_path)
    r = subprocess.run([sys.executable, HOOK, event],
                       input=json.dumps(_payload(name, tmp_path)),
                       capture_output=True, text=True,
                       env=_without_agent_env(env), timeout=30)
    assert (r.returncode, r.stdout) == (0, "")
    assert _state_files(home) == []


def test_cursor_hook_environment_alone_is_enough(tmp_path, monkeypatch):
    env, home = _enabled_env(tmp_path)
    env = dict(_without_agent_env(env), CURSOR_VERSION="2026.02.13")
    calls = _run_in_process(monkeypatch, "SessionStart",
                            _payload("claude_code_session_start.json", tmp_path),
                            env, home)
    assert calls == []


def test_claude_code_still_spawns_exporter_and_pinger(tmp_path, monkeypatch):
    env, home = _enabled_env(tmp_path)
    payload = _payload("claude_code_session_start.json", tmp_path)
    calls = _run_in_process(monkeypatch, "SessionStart", payload,
                            _without_agent_env(env), home)
    assert len(calls) == 2
    assert state.load(payload["session_id"], home).get("instance_id")


def test_claude_code_run_from_cursors_agent_shell_is_still_traced(
        tmp_path, monkeypatch):
    env, home = _enabled_env(tmp_path)
    env = dict(_without_agent_env(env), CURSOR_AGENT="1")
    calls = _run_in_process(monkeypatch, "SessionStart",
                            _payload("claude_code_session_start.json", tmp_path),
                            env, home)
    assert len(calls) == 2


def test_claude_code_run_from_a_codex_shell_is_still_traced(tmp_path):
    payload = _payload("claude_code_session_start.json", tmp_path)
    env = {"CODEX_HOME": str(tmp_path / "codex")}
    assert not foreign_agent.is_foreign(payload, env, str(tmp_path))


def test_a_transcript_under_codex_home_is_codex(tmp_path):
    codex_home = tmp_path / "codex"
    payload = {"session_id": "s",
               "transcript_path": str(codex_home / "sessions" / "s.jsonl")}
    assert foreign_agent.is_foreign(payload, {"CODEX_HOME": str(codex_home)},
                                    str(tmp_path))


def test_a_transcript_under_the_default_codex_home_is_codex(tmp_path):
    payload = {"session_id": "s",
               "transcript_path": str(tmp_path / ".codex" / "sessions" / "s.jsonl")}
    assert foreign_agent.is_foreign(payload, {}, str(tmp_path))


def test_a_missing_transcript_path_is_not_foreign(tmp_path):
    assert not foreign_agent.is_foreign({"session_id": "s",
                                         "transcript_path": None},
                                        {}, str(tmp_path))


def test_a_codex_home_above_claude_codes_home_is_not_codex(tmp_path):
    transcript = tmp_path / ".claude" / "projects" / "p" / "s.jsonl"
    payload = {"session_id": "s", "transcript_path": str(transcript)}
    assert not foreign_agent.is_foreign(payload, {"CODEX_HOME": str(tmp_path)},
                                        str(tmp_path))


def test_claude_config_dir_is_claude_codes_home(tmp_path):
    config_dir = tmp_path / "cc"
    payload = {"session_id": "s",
               "transcript_path": str(config_dir / "projects" / "s.jsonl")}
    env = {"CODEX_HOME": str(tmp_path), "CLAUDE_CONFIG_DIR": str(config_dir)}
    assert not foreign_agent.is_foreign(payload, env, str(tmp_path))
