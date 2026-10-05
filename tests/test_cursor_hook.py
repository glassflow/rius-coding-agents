"""hook.py --agent cursor: spool every event, answer Cursor on stdout, and
hand the exporter a job only when one is due."""
import io
import json
import os
import pathlib
import subprocess
import sys

import pytest

import hook as hook_mod
from rius_cc import agent, cursor_events, cursor_export, cursor_hook, state
from tests import cursor_fixtures
from tests.platforms import minimal_env

HOOK = str(pathlib.Path(__file__).parent.parent / "scripts" / "hook.py")
CID = "c0ffee00-0000-4000-8000-000000000001"


def _env(home, **extra):
    env = {"HOME": str(home), "USERPROFILE": str(home),
           "RIUS_API_KEY": "glassflow_k", "RIUS_ENDPOINT": "https://ingest.test",
           "RIUS_CURSOR_ENABLED": "true"}
    env.update(extra)
    return env


def _run(monkeypatch, capsys, event, payload, env):
    """hook.main() in this process with Popen faked. Returns (stdout, the
    exporter hand-off files it would have spawned with)."""
    spawned = []

    class FakePopen:
        def __init__(self, argv, **kw):
            with open(argv[2]) as fh:
                spawned.append({"argv": list(argv), "handoff": json.load(fh)})
            os.remove(argv[2])

    monkeypatch.setattr(hook_mod.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(os, "environ", dict(env))
    monkeypatch.setattr(sys, "argv", ["hook.py", "--agent", "cursor", event])
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    with agent.using(agent.CLAUDE_CODE):
        hook_mod.main()
    return capsys.readouterr().out, spawned


def _session(name="docs_session"):
    return cursor_fixtures.payloads(name)


def _spool_events(home, cid=CID):
    with agent.using(agent.CURSOR):
        return cursor_export.read_events(str(home), cid)


def test_every_event_is_answered_with_json(monkeypatch, capsys, tmp_path):
    for payload in _session():
        out, _ = _run(monkeypatch, capsys, payload["hook_event_name"],
                      payload, _env(tmp_path))
        assert isinstance(json.loads(out), dict), payload["hook_event_name"]


def test_session_start_hands_the_commands_their_session(monkeypatch, capsys,
                                                        tmp_path):
    out, _ = _run(monkeypatch, capsys, "sessionStart", _session()[0],
                  _env(tmp_path))
    session_env = json.loads(out)["env"]
    assert session_env[cursor_hook.SESSION_ENV] == CID
    scripts = os.path.join(session_env[cursor_hook.PLUGIN_ROOT_ENV], "scripts")
    assert os.path.exists(os.path.join(scripts, "rius_ctl.sh"))


def test_session_start_tells_the_agent_too(monkeypatch, capsys, tmp_path):
    # Cursor's CLI applies a sessionStart `env` to later hooks only, not to
    # the agent's Shell tool, so the commands fall back to this note.
    out, _ = _run(monkeypatch, capsys, "sessionStart", _session()[0],
                  _env(tmp_path))
    answer = json.loads(out)
    note = answer["additional_context"]
    assert note.startswith(cursor_hook.CONTEXT_NOTE_PREFIX)
    assert answer["env"][cursor_hook.PLUGIN_ROOT_ENV] in note
    assert CID in note


def test_a_full_session_lands_in_the_spool(monkeypatch, capsys, tmp_path):
    for payload in _session():
        _run(monkeypatch, capsys, payload["hook_event_name"], payload,
             _env(tmp_path))
    events = [e["event"] for e in _spool_events(tmp_path)]
    assert events == [p["hook_event_name"] for p in _session()]


def test_exports_only_when_due(monkeypatch, capsys, tmp_path):
    due = []
    for payload in _session():
        _, spawned = _run(monkeypatch, capsys, payload["hook_event_name"],
                          payload, _env(tmp_path))
        due += [s["handoff"]["event"] for s in spawned]
    assert due == ["sessionStart", "stop", "subagentStop", "stop", "sessionEnd"]


def test_the_handoff_names_the_root_and_carries_no_content(monkeypatch,
                                                           capsys, tmp_path):
    for payload in _session():
        _, spawned = _run(monkeypatch, capsys, payload["hook_event_name"],
                          payload, _env(tmp_path))
        for s in spawned:
            assert s["handoff"]["payload"] == {
                "conversation_id": CID, "cwd": "/Users/dev/acme-api"}
            assert s["argv"][-2:] == ["--agent", "cursor"]


def test_long_turns_export_every_n_tools(monkeypatch, capsys, tmp_path):
    post = {"conversation_id": CID, "hook_event_name": "postToolUse",
            "generation_id": "g", "tool_name": "Read"}
    due = 0
    for i in range(cursor_export.EXPORT_EVERY_N_TOOLS * 2):
        post["tool_use_id"] = "t%d" % i
        _, spawned = _run(monkeypatch, capsys, "postToolUse", post,
                          _env(tmp_path))
        due += len(spawned)
    assert due == 2


def test_subagent_tools_export_under_the_root(monkeypatch, capsys, tmp_path):
    payloads = _session()
    start = next(p for p in payloads if p["hook_event_name"] == "subagentStart")
    for payload in payloads[:payloads.index(start) + 1]:
        _run(monkeypatch, capsys, payload["hook_event_name"], payload,
             _env(tmp_path))
    with agent.using(agent.CURSOR):
        sdir = cursor_export.spool_dir(str(tmp_path))
        assert cursor_export.root_conversation(sdir, start["subagent_id"]) == CID


def test_disabled_folder_spools_nothing(monkeypatch, capsys, tmp_path):
    env = _env(tmp_path, RIUS_CURSOR_ENABLED="false")
    for payload in _session():
        out, spawned = _run(monkeypatch, capsys, payload["hook_event_name"],
                            payload, env)
        assert spawned == [] and json.loads(out) is not None
    assert _spool_events(tmp_path) == []


def test_no_key_spools_nothing(monkeypatch, capsys, tmp_path):
    env = _env(tmp_path)
    del env["RIUS_API_KEY"]
    for payload in _session():
        _, spawned = _run(monkeypatch, capsys, payload["hook_event_name"],
                          payload, env)
        assert spawned == []
    assert not (tmp_path / ".cursor" / "rius" / "spool").exists()


def test_disabled_mid_session_still_closes_the_open_trace(monkeypatch, capsys,
                                                          tmp_path):
    with agent.using(agent.CURSOR):
        state.save(CID, str(tmp_path), dict(state.new_state(),
                                            root_started=True))
    env = _env(tmp_path, RIUS_CURSOR_ENABLED="false")
    end = _session()[-1]
    _, spawned = _run(monkeypatch, capsys, "sessionEnd", end, env)
    assert [s["handoff"]["event"] for s in spawned] == ["sessionEnd"]
    assert _spool_events(tmp_path) == []


def test_nothing_reaches_claude_code_state(monkeypatch, capsys, tmp_path):
    for payload in _session():
        _run(monkeypatch, capsys, payload["hook_event_name"], payload,
             _env(tmp_path))
    assert not (tmp_path / ".claude").exists()


@pytest.mark.parametrize("raw", ["", "not json", "[1, 2]"])
def test_garbage_still_gets_an_answer(raw, tmp_path):
    r = subprocess.run([sys.executable, HOOK, "--agent", "cursor", "stop"],
                       input=raw, capture_output=True, text=True,
                       env=minimal_env(**_env(tmp_path)), timeout=30)
    assert r.returncode == 0
    assert json.loads(r.stdout) == {}


def test_the_spool_honours_capture_off(monkeypatch, capsys, tmp_path):
    env = _env(tmp_path, RIUS_CAPTURE_CONTENT="false")
    for payload in _session():
        _run(monkeypatch, capsys, payload["hook_event_name"], payload, env)
    with agent.using(agent.CURSOR):
        spool = cursor_events.spool_path(
            cursor_export.spool_dir(str(tmp_path)), CID)
    text = open(spool).read()
    assert "invoice totals test" not in text and "pytest" not in text
