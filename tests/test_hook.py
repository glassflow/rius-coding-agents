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


def test_resume_mints_a_fresh_instance_id_different_from_state(tmp_path, monkeypatch):
    """A resume is a new process lifetime. SessionEnd already sent a
    `stopped: true` ping for the old instance id, so reusing it would make
    the backend see a stopped instance start pinging again."""
    env, home = _enabled_env(tmp_path)
    sid = "hb-resume-1"
    with state.session_lock(sid, home):
        st = state.load(sid, home)
        st["instance_id"] = "old-instance-id"
        state.save(sid, home, st)

    _run_in_process(
        monkeypatch, "SessionStart",
        {"session_id": sid, "cwd": str(tmp_path), "source": "resume",
         "transcript_path": "/nonexistent.jsonl"}, env, home)

    new_id = state.load(sid, home).get("instance_id")
    assert new_id
    assert new_id != "old-instance-id"


def test_compact_keeps_the_existing_instance_id(tmp_path, monkeypatch):
    """Compaction is the same process continuing with a compacted context --
    not a new process lifetime."""
    env, home = _enabled_env(tmp_path)
    sid = "hb-compact-1"
    with state.session_lock(sid, home):
        st = state.load(sid, home)
        st["instance_id"] = "same-instance-id"
        state.save(sid, home, st)

    _run_in_process(
        monkeypatch, "SessionStart",
        {"session_id": sid, "cwd": str(tmp_path), "source": "compact",
         "transcript_path": "/nonexistent.jsonl"}, env, home)

    assert state.load(sid, home).get("instance_id") == "same-instance-id"


def test_clear_keeps_the_existing_instance_id(tmp_path, monkeypatch):
    env, home = _enabled_env(tmp_path)
    sid = "hb-clear-1"
    with state.session_lock(sid, home):
        st = state.load(sid, home)
        st["instance_id"] = "same-instance-id"
        state.save(sid, home, st)

    _run_in_process(
        monkeypatch, "SessionStart",
        {"session_id": sid, "cwd": str(tmp_path), "source": "clear",
         "transcript_path": "/nonexistent.jsonl"}, env, home)

    assert state.load(sid, home).get("instance_id") == "same-instance-id"


def test_startup_keeps_existing_id_or_mints_if_absent(tmp_path, monkeypatch):
    env, home = _enabled_env(tmp_path)
    sid = "hb-startup-1"
    with state.session_lock(sid, home):
        st = state.load(sid, home)
        st["instance_id"] = "same-instance-id"
        state.save(sid, home, st)

    _run_in_process(
        monkeypatch, "SessionStart",
        {"session_id": sid, "cwd": str(tmp_path), "source": "startup",
         "transcript_path": "/nonexistent.jsonl"}, env, home)
    assert state.load(sid, home).get("instance_id") == "same-instance-id"

    sid2 = "hb-startup-2"
    assert not state.load(sid2, home).get("instance_id")
    _run_in_process(
        monkeypatch, "SessionStart",
        {"session_id": sid2, "cwd": str(tmp_path), "source": "startup",
         "transcript_path": "/nonexistent.jsonl"}, env, home)
    assert state.load(sid2, home).get("instance_id")


def test_resume_hands_both_spawned_children_the_new_instance_id(tmp_path, monkeypatch):
    env, home = _enabled_env(tmp_path)
    sid = "hb-resume-2"
    with state.session_lock(sid, home):
        st = state.load(sid, home)
        st["instance_id"] = "old-instance-id"
        state.save(sid, home, st)

    calls = _run_in_process(
        monkeypatch, "SessionStart",
        {"session_id": sid, "cwd": str(tmp_path), "source": "resume",
         "transcript_path": "/nonexistent.jsonl"}, env, home)

    new_id = state.load(sid, home).get("instance_id")
    assert new_id and new_id != "old-instance-id"
    assert len(calls) == 2
    for call in calls:
        assert "old-instance-id" not in call["argv"]
        assert new_id in call["argv"]
        assert call["instance_id_in_state"] == new_id


def test_resume_does_not_change_the_session_id(tmp_path, monkeypatch):
    """The session id (and therefore the trace id) is unchanged across a
    resume -- only the instance changes."""
    env, home = _enabled_env(tmp_path)
    sid = "hb-resume-3"
    with state.session_lock(sid, home):
        st = state.load(sid, home)
        st["instance_id"] = "old-instance-id"
        state.save(sid, home, st)

    _run_in_process(
        monkeypatch, "SessionStart",
        {"session_id": sid, "cwd": str(tmp_path), "source": "resume",
         "transcript_path": "/nonexistent.jsonl"}, env, home)

    # the session id in state is still the same key -- a resume never
    # migrates content to a new session id, so the trace id is unchanged.
    assert state.load(sid, home).get("instance_id")


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


# --- Cross-platform spawn and parent lookup ---------------------------------

import json as _json  # noqa: E402
from rius_cc import platform_compat  # noqa: E402
from tests.test_platform_compat import FakeMsvcrt  # noqa: E402

HOOK_SH = str(pathlib.Path(__file__).parent.parent / "scripts" / "hook.sh")
HOOKS_JSON = pathlib.Path(__file__).parent.parent / "hooks" / "hooks.json"


def test_detached_spawn_kwargs_reach_popen_on_posix(tmp_path, monkeypatch):
    """TRAP 3: the child must outlive this hook process. On POSIX that is
    setsid, and it is the only reason the exporter survives at all."""
    env, home = _enabled_env(tmp_path)
    calls = _run_in_process(
        monkeypatch, "PostToolUse",
        {"session_id": "detach-1", "cwd": str(tmp_path),
         "transcript_path": "/nonexistent.jsonl"}, env, home)
    assert calls[0]["kwargs"]["start_new_session"] is True
    assert "creationflags" not in calls[0]["kwargs"]


def test_detached_spawn_kwargs_reach_popen_on_windows(tmp_path, monkeypatch):
    """start_new_session is POSIX-only. Windows needs creationflags, and a
    hook that passed start_new_session there would spawn nothing."""
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    # The session lock now goes through msvcrt too -- without this the hook
    # would swallow the ImportError and spawn nothing, which is precisely
    # the silent failure being fixed.
    monkeypatch.setitem(sys.modules, "msvcrt", FakeMsvcrt())
    env, home = _enabled_env(tmp_path)
    calls = _run_in_process(
        monkeypatch, "SessionStart",
        {"session_id": "detach-2", "cwd": str(tmp_path),
         "transcript_path": "/nonexistent.jsonl"}, env, home)
    assert len(calls) == 2
    for call in calls:
        kwargs = call["kwargs"]
        assert "start_new_session" not in kwargs
        flags = kwargs["creationflags"]
        assert flags & platform_compat.DETACHED_PROCESS
        assert flags & platform_compat.CREATE_NEW_PROCESS_GROUP


def test_claude_code_pid_never_falls_back_to_the_dying_shell(monkeypatch):
    """TRAP 5. The immediate parent is the shell hooks.json spawned, and it
    exits the moment hook.py does. Handing the pinger that pid makes it see
    a dead parent on its first iteration and exit -- a session that silently
    produces no heartbeats, which is a bug this code path has had before. A
    recycled pid would be worse still: the pinger would watch a stranger.
    0 means 'unwatched', and the pinger says so."""
    monkeypatch.setattr(platform_compat, "parent_pid_of", lambda _pid: 0)
    got = hook_mod._claude_code_pid()
    assert got == 0
    assert got != os.getppid()


def test_claude_code_pid_uses_the_grandparent_when_known(monkeypatch):
    monkeypatch.setattr(platform_compat, "parent_pid_of", lambda _pid: 4242)
    assert hook_mod._claude_code_pid() == 4242


def test_claude_code_pid_survives_a_raising_lookup(monkeypatch):
    def boom(_pid):
        raise OSError("no ps, no toolhelp")
    monkeypatch.setattr(platform_compat, "parent_pid_of", boom)
    assert hook_mod._claude_code_pid() == 0


# --- TRAP 4: how the hook is invoked at all ---------------------------------

def test_hooks_json_does_not_rely_on_the_shebang():
    """Windows has no shebang support, so a hooks.json that executes
    hook.py directly is a plugin that does nothing there, silently."""
    wired = _json.loads(HOOKS_JSON.read_text())["hooks"]
    assert wired, "no hook events wired"
    for event, groups in wired.items():
        for group in groups:
            for entry in group["hooks"]:
                command = entry["command"]
                assert "hook.sh" in command, \
                    "%s is invoked without the interpreter-resolving launcher" % event
                assert command.endswith(" " + event), command
                assert entry["shell"] == "bash", \
                    "%s must declare its shell rather than inherit one" % event


def test_launcher_runs_the_hook_end_to_end(tmp_path):
    """The launcher is what Claude Code actually executes, so it -- not just
    hook.py -- has to produce the hook's effects."""
    env, home = _enabled_env(tmp_path)
    sid = "launcher-1"
    r = subprocess.run(
        ["bash", HOOK_SH, "SessionEnd"],
        input=_json.dumps({"session_id": sid, "cwd": str(tmp_path),
                           "transcript_path": "/nonexistent.jsonl"}),
        capture_output=True, text=True, env=env, timeout=30)
    assert r.returncode == 0
    assert r.stdout.strip() == "", "the launcher wrote to the hook control channel"
    assert os.path.exists(os.path.join(state.state_dir(home),
                                       sid + ".heartbeat.stop"))


def test_launcher_exits_zero_and_silent_on_garbage(tmp_path):
    env, _home = _enabled_env(tmp_path)
    r = subprocess.run(["bash", HOOK_SH, "Stop"], input="not json",
                       capture_output=True, text=True, env=env, timeout=30)
    assert r.returncode == 0
    assert r.stdout.strip() == ""


def test_launcher_says_so_when_no_interpreter_exists(tmp_path):
    """A hook that can find no Python can only do one useful thing: leave a
    breadcrumb. Exiting 0 with nothing written is the failure mode this
    whole plugin exists to avoid."""
    home = tmp_path / "home"
    home.mkdir()
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    # A PATH with the shell's own utilities but no python of any name.
    for tool in ("mkdir", "date", "cat"):
        for candidate in ("/bin/" + tool, "/usr/bin/" + tool):
            if os.path.exists(candidate):
                os.symlink(candidate, str(fakebin / tool))
                break
    r = subprocess.run(["/bin/sh", HOOK_SH, "Stop"], input="{}",
                       capture_output=True, text=True, timeout=30,
                       env={"PATH": str(fakebin), "HOME": str(home)})
    assert r.returncode == 0
    assert r.stdout.strip() == ""
    log = home / ".claude" / "rius" / "log" / "bootstrap.log"
    assert log.exists(), "no Python and no explanation anywhere"
    assert "no Python interpreter found" in log.read_text()
