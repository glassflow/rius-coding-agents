import io
import json
import os
import subprocess
import sys
import time
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "scripts"))

import hook as hook_mod  # noqa: E402
from rius_cc import config, state  # noqa: E402

HOOK = str(pathlib.Path(__file__).parent.parent / "scripts" / "hook.py")


def _run_in_process(monkeypatch, event, payload, env, home):
    """Run hook.main() here, with Popen faked, so the children are never
    really spawned. Records each spawn's argv AND what was already persisted
    to state at the moment of that spawn -- which is how the ordering
    guarantee (mint before spawn) is asserted rather than assumed."""
    calls = []
    sid = payload.get("session_id", "")

    class FakePopen:
        def __init__(self, argv, **kw):
            argv = list(argv)
            # hook.py also shells out to `ps` to find Claude Code's pid;
            # only the two detached children are of interest here.
            if not any(isinstance(a, str) and a.endswith(
                    ("exporter.py", "heartbeat.py")) for a in argv):
                raise OSError("no ps in this test")
            calls.append({
                "argv": argv,
                "kwargs": kw,
                "instance_id_in_state": state.load(sid, home).get("instance_id"),
            })

    monkeypatch.setattr(hook_mod.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(os, "environ", dict(env))
    monkeypatch.setattr(sys, "argv", ["hook.py", event])
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    hook_mod.main()
    for call in calls:                      # the temp payload file we faked away
        for arg in call["argv"]:
            if isinstance(arg, str) and arg.endswith(".json") and "rius-hook-" in arg:
                try:
                    os.remove(arg)
                except OSError:
                    pass
    return calls


def _run(event, payload, env=None):
    return subprocess.run(
        [sys.executable, HOOK, event],
        input=json.dumps(payload), capture_output=True, text=True,
        env=env, timeout=30)


def _enabled_env(tmp_path):
    home = tmp_path / "home"
    (home / ".claude" / "rius").mkdir(parents=True)
    with open(config.path_rules_path(str(home)), "w") as fh:
        json.dump({"enabled_paths": [str(tmp_path)]}, fh)
    env = dict(os.environ)
    env.update({
        "HOME": str(home),
        "RIUS_API_KEY": "glassflow_k",
        "RIUS_ENDPOINT": "https://ingest.test",
    })
    return env, str(home)


def test_exits_zero_on_a_normal_payload(tmp_path):
    r = _run("SessionStart", {"session_id": "s1", "cwd": str(tmp_path),
                              "transcript_path": "/nonexistent.jsonl"})
    assert r.returncode == 0


def test_exits_zero_on_invalid_json_stdin():
    r = subprocess.run([sys.executable, HOOK, "Stop"], input="not json",
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0


def test_exits_zero_on_empty_stdin():
    r = subprocess.run([sys.executable, HOOK, "Stop"], input="",
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0


def test_exits_zero_with_no_argument():
    r = subprocess.run([sys.executable, HOOK], input="{}",
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0


def test_prints_nothing_on_stdout_when_disabled(tmp_path):
    """stdout is a control channel for hooks -- stray output is a protocol bug."""
    r = _run("Stop", {"session_id": "s1", "cwd": str(tmp_path),
                      "transcript_path": "/nonexistent.jsonl"})
    assert r.stdout.strip() == ""


def test_returns_fast(tmp_path):
    import time
    start = time.time()
    _run("PostToolUse", {"session_id": "s1", "cwd": str(tmp_path),
                         "transcript_path": "/nonexistent.jsonl"})
    assert time.time() - start < 2.0


def test_session_start_mints_instance_id_before_spawning_either_child(tmp_path, monkeypatch):
    """C1: the pinger only pings if it has an instance id. Nothing may be
    spawned before that id exists, and both children must be handed it --
    otherwise the pinger loses a startup race with the exporter and every
    fresh session silently produces zero heartbeats."""
    env, home = _enabled_env(tmp_path)
    sid = "hb-order-1"
    assert not state.load(sid, home).get("instance_id")   # brand-new session

    calls = _run_in_process(
        monkeypatch, "SessionStart",
        {"session_id": sid, "cwd": str(tmp_path),
         "transcript_path": "/nonexistent.jsonl"}, env, home)

    assert len(calls) == 2, "expected an exporter AND a heartbeat pinger"
    instance_id = state.load(sid, home).get("instance_id")
    assert instance_id, "hook.py did not persist an instance id"
    for call in calls:
        assert call["instance_id_in_state"] == instance_id, \
            "a child was spawned before the instance id was minted"
        assert instance_id in call["argv"], \
            "child did not receive the instance id on argv: %r" % (call["argv"],)
    # the pinger gets it as its last argument, after the watched pid
    assert calls[1]["argv"][-1] == instance_id


def test_non_session_start_does_not_spawn_a_pinger_or_mint_eagerly(tmp_path, monkeypatch):
    env, home = _enabled_env(tmp_path)
    sid = "hb-order-2"
    calls = _run_in_process(
        monkeypatch, "PostToolUse",
        {"session_id": sid, "cwd": str(tmp_path),
         "transcript_path": "/nonexistent.jsonl"}, env, home)
    assert len(calls) == 1   # exporter only


def test_session_start_removes_a_stale_stop_file(tmp_path, monkeypatch):
    """C2: nothing else ever deletes the stop file. A resumed or cleared
    session would otherwise hand the fresh pinger an old stop signal, which
    reports `stopped: true` for a session that is very much alive."""
    env, home = _enabled_env(tmp_path)
    sid = "hb-stale-stop"
    stop_path = os.path.join(state.state_dir(home), sid + ".heartbeat.stop")
    with open(stop_path, "w") as fh:
        fh.write("")

    _run_in_process(monkeypatch, "SessionStart",
                    {"session_id": sid, "cwd": str(tmp_path),
                     "transcript_path": "/nonexistent.jsonl"}, env, home)
    assert not os.path.exists(stop_path), "stale stop file survived SessionStart"


def test_session_start_spawns_heartbeat_pinger(tmp_path):
    """A pinger writes a pid file once it starts. Nothing is pre-seeded:
    hook.py mints the instance id itself, so the real spawn path is what is
    under test here."""
    env, home = _enabled_env(tmp_path)
    sid = "hb-session-1"

    r = _run("SessionStart", {"session_id": sid, "cwd": str(tmp_path),
                              "transcript_path": "/nonexistent.jsonl"}, env=env)
    assert r.returncode == 0

    pid_path = os.path.join(state.state_dir(home), sid + ".heartbeat.pid")
    deadline = time.time() + 5
    while time.time() < deadline and not os.path.exists(pid_path):
        time.sleep(0.1)
    assert os.path.exists(pid_path), "heartbeat pinger never started"
    assert state.load(sid, home).get("instance_id"), \
        "the pinger started without an instance id to ping with"

    # Clean up: tell it to stop so it doesn't linger past the test. The
    # pinger clears a stale stop file at startup, so keep re-writing it
    # until the pid file disappears.
    stop_path = os.path.join(state.state_dir(home), sid + ".heartbeat.stop")
    deadline = time.time() + 10
    while time.time() < deadline and os.path.exists(pid_path):
        with open(stop_path, "w") as fh:
            fh.write("")
        time.sleep(0.2)
    assert not os.path.exists(pid_path), "heartbeat pinger never exited"


def test_session_end_writes_stop_file(tmp_path):
    env, home = _enabled_env(tmp_path)
    sid = "hb-session-2"
    r = _run("SessionEnd", {"session_id": sid, "cwd": str(tmp_path),
                            "transcript_path": "/nonexistent.jsonl"}, env=env)
    assert r.returncode == 0
    stop_path = os.path.join(state.state_dir(home), sid + ".heartbeat.stop")
    assert os.path.exists(stop_path)


def test_session_end_does_not_spawn_heartbeat(tmp_path):
    env, home = _enabled_env(tmp_path)
    sid = "hb-session-3"
    _run("SessionEnd", {"session_id": sid, "cwd": str(tmp_path),
                        "transcript_path": "/nonexistent.jsonl"}, env=env)
    pid_path = os.path.join(state.state_dir(home), sid + ".heartbeat.pid")
    time.sleep(0.3)
    assert not os.path.exists(pid_path)
