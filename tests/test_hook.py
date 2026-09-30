import atexit
import io
import json
import os
import shutil
import tempfile
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


def _isolated_env():
    """os.environ with a fresh, throwaway HOME and no RIUS_* settings.

    hook.py resolves its home from HOME: run with the developer's own
    environment it reads their real key and writes into their real
    ~/.claude/rius/state. No test may do that.
    """
    home = tempfile.mkdtemp(prefix="rius-test-home-")
    atexit.register(shutil.rmtree, home, True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("RIUS_")}
    env["HOME"] = home
    env["USERPROFILE"] = home
    return env


def _run(event, payload, env=None):
    if env is None:
        env = _isolated_env()
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
    walked_from = []

    def unknown(pid):
        walked_from.append(pid)
        return 0

    monkeypatch.setattr(platform_compat, "parent_pid_of", unknown)
    assert hook_mod._claude_code_pid() == 0
    # Not entailed by the line above, and the part that actually breaks: the
    # walk has to START at this process's parent. hook.sh `exec`s hook.py, so
    # this pid is the launcher's; one level up is the shell Claude Code
    # spawned, and parent_pid_of takes it from there.
    assert walked_from == [os.getppid()]


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


def _fakebin(tmp_path, **stubs):
    """A PATH directory of stub interpreters, one per keyword.

    ``real`` execs the interpreter running this test; ``fail`` exits
    non-zero without being a Python, which is what the Microsoft Store's
    App Execution Alias amounts to here -- something `command -v` finds,
    that is not Python, and that opens the Store instead of running code.
    """
    d = tmp_path / "fakebin"
    d.mkdir(exist_ok=True)
    for name, kind in stubs.items():
        body = ('#!/bin/sh\nexec "%s" "$@"\n' % sys.executable
                if kind == "real" else '#!/bin/sh\nexit 1\n')
        stub = d / name
        stub.write_text(body)
        stub.chmod(0o755)
    return str(d)


def _run_launcher(env, sid, tmp_path, event="SessionEnd"):
    return subprocess.run(
        ["/bin/bash", HOOK_SH, event],
        input=_json.dumps({"session_id": sid, "cwd": str(tmp_path),
                           "transcript_path": "/nonexistent.jsonl"}),
        capture_output=True, text=True, env=env, timeout=30)


def _stopped(home, sid):
    return os.path.exists(os.path.join(state.state_dir(home),
                                       sid + ".heartbeat.stop"))


def test_hook_py_must_not_carry_a_shebang():
    """C1. `hook.sh` picks `py` on Windows precisely to dodge the Store
    alias -- and then hands it a script whose first line is
    `#!/usr/bin/env python3`. The PEP 397 launcher honours that shebang by
    PATH-searching for `python3` BEFORE consulting its own registered
    interpreters, and the only `python3` on a default Windows PATH is the
    Store alias. So `py` re-dispatches straight back into the stub the
    candidate order exists to avoid: a Store window per hook event, hook.py
    never running, and a non-zero exit. Nothing invokes this file by
    shebang any more -- hooks.json goes through hook.sh and the suite uses
    sys.executable -- so the line has no upside left."""
    first = pathlib.Path(HOOK).read_text().splitlines()[0]
    assert not first.startswith("#!"), (
        "hook.py carries a shebang: on Windows `py` will honour it and "
        "re-dispatch to the Microsoft Store alias -- %r" % first)


def test_launcher_prefers_py_over_the_store_alias_on_windows(tmp_path):
    """I3. Windows candidate order, exercised without a Windows.

    `py` is a real binary or absent; `python`/`python3` are usually the
    Store aliases. If the order ever flips, this is the only thing that
    notices before a user does."""
    env, home = _enabled_env(tmp_path)
    env.update({"OS": "Windows_NT",
                "PATH": _fakebin(tmp_path, py="real", python="fail",
                                 python3="fail")})
    sid = "win-launcher-1"
    r = _run_launcher(env, sid, tmp_path)
    assert r.returncode == 0
    assert r.stdout.strip() == "", "the launcher wrote to the hook control channel"
    assert _stopped(home, sid), "the launcher did not reach hook.py through `py`"


def test_launcher_detects_windows_without_OS_in_the_environment(tmp_path):
    """I1. `$OS` was the sole gate, and nothing guarantees Claude Code hands
    a hook shell a full environment. A missing `$OS` on Windows took the
    POSIX branch, put the Store alias first and skipped the probe that would
    have rejected it -- silently, because it did find "a Python"."""
    env, home = _enabled_env(tmp_path)
    env.pop("OS", None)
    env.update({"WINDIR": "C:\\WINDOWS",
                "PATH": _fakebin(tmp_path, py="real", python="fail",
                                 python3="fail")})
    sid = "win-launcher-2"
    r = _run_launcher(env, sid, tmp_path)
    assert r.returncode == 0
    assert _stopped(home, sid), "$OS is still the only Windows signal"


def test_launcher_probes_every_candidate_on_every_platform(tmp_path):
    """I1. The probe used to be Windows-only, so a `python3` on PATH that is
    not a Python was exec'd on its name alone. One `python -c ''` in a path
    that is about to spawn a Python anyway buys correctness that does not
    depend on guessing the platform right."""
    env, home = _enabled_env(tmp_path)
    env.pop("OS", None)
    env["PATH"] = _fakebin(tmp_path, python3="fail", python="real")
    sid = "probe-1"
    r = _run_launcher(env, sid, tmp_path)
    assert r.returncode == 0
    assert _stopped(home, sid), "a non-Python `python3` was exec'd instead of skipped"


def test_launcher_probe_does_not_eat_the_hook_payload(tmp_path):
    """M6. The probe runs a candidate that may be anything at all; without
    `< /dev/null` it inherits the hook payload on stdin and can drain it,
    leaving the real interpreter with nothing to parse."""
    env, home = _enabled_env(tmp_path)
    env.pop("OS", None)
    drain = tmp_path / "fakebin"
    drain.mkdir(exist_ok=True)
    stub = drain / "python3"
    stub.write_text("#!/bin/sh\ncat >/dev/null\nexit 1\n")
    stub.chmod(0o755)
    real = drain / "python"
    real.write_text('#!/bin/sh\nexec "%s" "$@"\n' % sys.executable)
    real.chmod(0o755)
    env["PATH"] = str(drain)
    sid = "drain-1"
    r = _run_launcher(env, sid, tmp_path)
    assert r.returncode == 0
    assert _stopped(home, sid), "the probe swallowed the hook payload"


def test_launcher_with_no_usable_python_exits_zero_and_says_so(tmp_path):
    """Silence is the failure mode this plugin exists to avoid, and a hook
    may not exit non-zero to complain."""
    env, home = _enabled_env(tmp_path)
    env.pop("OS", None)
    env["PATH"] = (_fakebin(tmp_path, python3="fail", python="fail")
                   + os.pathsep + "/usr/bin" + os.pathsep + "/bin")
    r = _run_launcher(env, "nopython-1", tmp_path)
    assert r.returncode == 0
    assert r.stdout.strip() == ""
    crumb = pathlib.Path(home) / ".claude" / "rius" / "log" / "bootstrap.log"
    assert crumb.exists(), "no interpreter and no breadcrumb either"
    assert "no Python" in crumb.read_text()


def test_launcher_names_a_missing_find_python_sh_instead_of_blaming_path(tmp_path):
    """Minor 1. If scripts/_find_python.sh is absent (a packaging fault, not
    a PATH problem), the guard around sourcing it must not silently leave
    rius_candidates unset -- that produces "tried: " with an empty list and
    misdiagnoses a missing file as a PATH problem. Copy hook.sh alone into a
    directory with no _find_python.sh and assert the breadcrumb names the
    real fault, with exit 0 preserved."""
    home = tmp_path / "home"
    home.mkdir()
    bare = tmp_path / "bare_scripts"
    bare.mkdir()
    import shutil
    shutil.copy(HOOK_SH, str(bare / "hook.sh"))
    # deliberately no hook.py, no _find_python.sh copied alongside
    r = subprocess.run(["/bin/sh", str(bare / "hook.sh"), "Stop"], input="{}",
                       capture_output=True, text=True, timeout=30,
                       env={"PATH": os.environ["PATH"], "HOME": str(home)})
    assert r.returncode == 0
    assert r.stdout.strip() == ""
    log = home / ".claude" / "rius" / "log" / "bootstrap.log"
    assert log.exists()
    contents = log.read_text()
    assert "_find_python.sh" in contents, (
        "breadcrumb did not name the missing file: %r" % contents)
    assert "tried: )" not in contents, (
        "breadcrumb still shows the empty-candidate-list PATH misdiagnosis")


def test_launcher_does_not_guess_its_directory(tmp_path):
    """M4. `dir=.` was a silent wrong answer: with $0 carrying no separator
    the launcher would run whatever `./hook.py` happens to be, or nothing,
    and say neither. Not knowing where you are is a breadcrumb, not a
    fallback."""
    env, home = _enabled_env(tmp_path)
    r = subprocess.run(
        ["/bin/bash", "hook.sh", "SessionEnd"],
        cwd=str(pathlib.Path(HOOK_SH).parent),
        input=_json.dumps({"session_id": "bare-argv0", "cwd": str(tmp_path),
                           "transcript_path": "/nonexistent.jsonl"}),
        capture_output=True, text=True, env=env, timeout=30)
    assert r.returncode == 0
    assert r.stdout.strip() == ""
    crumb = pathlib.Path(home) / ".claude" / "rius" / "log" / "bootstrap.log"
    assert crumb.exists() and "hook.sh" in crumb.read_text()


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


# --- a session disabled mid-way still gets its SessionEnd ---------------------

def _disabled_env_with_open_trace(tmp_path, sid, open_trace=True, key=True):
    env, home = _enabled_env(tmp_path)
    with open(config.path_rules_path(home), "w") as fh:
        json.dump({"enabled_paths": [str(tmp_path)],
                   "disabled_paths": [str(tmp_path / "proj")]}, fh)
    if open_trace:
        state.save(sid, home, {"root_started": True, "instance_id": "i-1"})
    if not key:
        env.pop("RIUS_API_KEY")
    return env, home


def _payload_in_disabled(tmp_path, sid):
    return {"session_id": sid, "cwd": str(tmp_path / "proj"),
            "transcript_path": "/nonexistent.jsonl"}


def _spawned(calls):
    return [pathlib.Path(c["argv"][1]).name for c in calls]


def test_a_disabled_session_with_an_open_trace_still_reaches_the_exporter(
        tmp_path, monkeypatch):
    sid = "stopped-1"
    env, home = _disabled_env_with_open_trace(tmp_path, sid)
    for event in ("PostToolUse", "Stop", "SessionEnd"):
        calls = _run_in_process(monkeypatch, event,
                                _payload_in_disabled(tmp_path, sid), env, home)
        assert _spawned(calls) == ["exporter.py"], event


def test_session_end_of_a_stopped_session_stops_its_pinger(tmp_path, monkeypatch):
    sid = "stopped-2"
    env, home = _disabled_env_with_open_trace(tmp_path, sid)
    _run_in_process(monkeypatch, "SessionEnd",
                    _payload_in_disabled(tmp_path, sid), env, home)
    assert os.path.exists(os.path.join(state.state_dir(home),
                                       sid + ".heartbeat.stop"))


def test_a_stopped_session_starting_again_gets_no_pinger(tmp_path, monkeypatch):
    sid = "stopped-3"
    env, home = _disabled_env_with_open_trace(tmp_path, sid)
    calls = _run_in_process(monkeypatch, "SessionStart",
                            dict(_payload_in_disabled(tmp_path, sid),
                                 source="resume"), env, home)
    assert _spawned(calls) == ["exporter.py"]
    assert state.load(sid, home)["instance_id"] == "i-1"


def test_a_disabled_session_never_traced_spawns_nothing(tmp_path, monkeypatch):
    sid = "stopped-4"
    env, home = _disabled_env_with_open_trace(tmp_path, sid, open_trace=False)
    calls = _run_in_process(monkeypatch, "SessionEnd",
                            _payload_in_disabled(tmp_path, sid), env, home)
    assert calls == []
    assert not os.path.exists(os.path.join(state.state_dir(home),
                                           sid + ".heartbeat.stop"))


def test_a_closed_trace_is_not_reopened_by_a_disabled_session(tmp_path,
                                                              monkeypatch):
    sid = "stopped-5"
    env, home = _disabled_env_with_open_trace(tmp_path, sid)
    state.save(sid, home, {"root_started": True, "finalized": True})
    assert _run_in_process(monkeypatch, "SessionEnd",
                           _payload_in_disabled(tmp_path, sid), env, home) == []


def test_a_disabled_session_without_a_key_spawns_nothing(tmp_path, monkeypatch):
    sid = "stopped-6"
    env, home = _disabled_env_with_open_trace(tmp_path, sid, key=False)
    assert _run_in_process(monkeypatch, "SessionEnd",
                           _payload_in_disabled(tmp_path, sid), env, home) == []
