import json
import os
import subprocess
import sys
import time
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "scripts"))

from rius_cc import config, state  # noqa: E402

HOOK = str(pathlib.Path(__file__).parent.parent / "scripts" / "hook.py")


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


def test_session_start_spawns_heartbeat_pinger(tmp_path):
    """A pinger writes a pid file once it starts; we pre-seed instance_id
    (normally minted by the exporter) so the pinger doesn't exit immediately
    for lack of one, and wait briefly for the detached process to start."""
    env, home = _enabled_env(tmp_path)
    sid = "hb-session-1"
    st = state.new_state()
    st["instance_id"] = "abc-123"
    state.save(sid, home, st)

    r = _run("SessionStart", {"session_id": sid, "cwd": str(tmp_path),
                              "transcript_path": "/nonexistent.jsonl"}, env=env)
    assert r.returncode == 0

    pid_path = os.path.join(state.state_dir(home), sid + ".heartbeat.pid")
    deadline = time.time() + 5
    while time.time() < deadline and not os.path.exists(pid_path):
        time.sleep(0.1)
    assert os.path.exists(pid_path), "heartbeat pinger never started"

    # Clean up: tell it to stop so it doesn't linger past the test.
    stop_path = os.path.join(state.state_dir(home), sid + ".heartbeat.stop")
    with open(stop_path, "w") as fh:
        fh.write("")
    deadline = time.time() + 5
    while time.time() < deadline and os.path.exists(pid_path):
        time.sleep(0.1)


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
