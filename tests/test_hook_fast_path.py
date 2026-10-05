"""RIUS-1224: the plugin never slows down a session.

On a machine with no stored Rius key, hook.py resolves no key and returns,
so hook.sh exits before starting any Python. Every hook declares a timeout,
and the Command Line Tools stub on macOS is never probed."""
import json
import os
import pathlib
import re
import subprocess
import time

import pytest

from tests.platforms import BASH, minimal_env, posix_only

ROOT = pathlib.Path(__file__).parent.parent
HOOK_SH = str(ROOT / "scripts" / "hook.sh")
FIND_PYTHON_SH = str(ROOT / "scripts" / "_find_python.sh")
HOOKS_JSON = ROOT / "hooks" / "hooks.json"

OFF_PATH_EVENTS = ("UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop",
                   "SessionEnd")


def _recording_interpreters(tmp_path):
    """Every interpreter name hook.sh may try, each one logging that it ran."""
    bindir = tmp_path / "fakebin"
    bindir.mkdir(exist_ok=True)
    log = tmp_path / "python-started"
    for name in ("python3", "python", "py"):
        stub = bindir / name
        stub.write_text('#!/bin/sh\necho "$0" >> "%s"\nexit 0\n'
                        % str(log).replace("\\", "/"))
        stub.chmod(0o755)
    return str(bindir), log


def _home(tmp_path, notice_shown=True, agent_dir=".claude"):
    rius = tmp_path / "home" / agent_dir / "rius"
    rius.mkdir(parents=True)
    if notice_shown:
        (rius / "install-notice-shown").write_text("")
    return rius


def _hook(tmp_path, *argv, **env):
    ran, elapsed, stdout = _hook_out(tmp_path, *argv, **env)
    assert stdout == ""
    return ran, elapsed


def _hook_out(tmp_path, *argv, **env):
    bindir, log = _recording_interpreters(tmp_path)
    base = {"PATH": bindir + os.pathsep + os.environ["PATH"],
            "HOME": str(tmp_path / "home"), "USERPROFILE": ""}
    base.update(env)
    started = time.monotonic()
    r = subprocess.run([BASH, HOOK_SH] + list(argv),
                       input=json.dumps({"session_id": "s1", "cwd": "/x"}),
                       capture_output=True, text=True, timeout=30,
                       env=minimal_env(**base))
    assert r.returncode == 0, r.stderr
    return log.exists(), time.monotonic() - started, r.stdout


@pytest.mark.parametrize("event", OFF_PATH_EVENTS + ("SessionStart",))
def test_signed_out_hooks_start_no_python(tmp_path, event):
    _home(tmp_path)
    ran, _ = _hook(tmp_path, event)
    assert not ran


@pytest.mark.parametrize("event", OFF_PATH_EVENTS)
def test_enabled_folders_without_a_key_still_skip_python_on_tool_events(
        tmp_path, event):
    (_home(tmp_path) / "config.json").write_text('{"enabled_paths": ["/x"]}')
    ran, _ = _hook(tmp_path, event)
    assert not ran


@pytest.mark.parametrize("event", OFF_PATH_EVENTS + ("SessionStart",))
def test_an_env_key_alone_starts_no_python(tmp_path, event):
    # hook.py ignores RIUS_API_KEY, so it is no reason to start Python.
    _home(tmp_path)
    ran, _ = _hook(tmp_path, event, RIUS_API_KEY="glassflow_k")
    assert not ran


AGENT_DIRS = {"codex": ".codex", "cursor": ".cursor"}
AGENT_EVENTS = {"codex": ("SessionStart", "PostToolUse", "Stop"),
                "cursor": ("postToolUse", "stop", "sessionEnd")}
SIGNED_OUT_ANSWER = {"codex": "", "cursor": "{}"}


@pytest.mark.parametrize("name", sorted(AGENT_DIRS))
def test_a_signed_out_agent_starts_no_python(tmp_path, name):
    _home(tmp_path, agent_dir=AGENT_DIRS[name])
    for event in AGENT_EVENTS[name]:
        ran, _, out = _hook_out(tmp_path, "--agent", name, event)
        assert not ran and out == SIGNED_OUT_ANSWER[name], event


@pytest.mark.parametrize("name", sorted(AGENT_DIRS))
def test_an_agent_with_its_own_key_starts_python(tmp_path, name):
    (_home(tmp_path, agent_dir=AGENT_DIRS[name])
     / "credentials.json").write_text('{"api_key": "ri_x"}')
    ran, _, _ = _hook_out(tmp_path, "--agent", name, AGENT_EVENTS[name][1])
    assert ran


@pytest.mark.parametrize("name", sorted(AGENT_DIRS))
def test_an_agent_with_an_open_trace_starts_python(tmp_path, name):
    state = _home(tmp_path, agent_dir=AGENT_DIRS[name]) / "state"
    state.mkdir()
    (state / "other-session.open").write_text("")
    ran, _, _ = _hook_out(tmp_path, "--agent", name, AGENT_EVENTS[name][2])
    assert ran


@pytest.mark.parametrize("name", sorted(AGENT_DIRS))
def test_one_agents_key_starts_no_python_for_another(tmp_path, name):
    (_home(tmp_path) / "credentials.json").write_text('{"api_key": "ri_x"}')
    _home(tmp_path, agent_dir=AGENT_DIRS[name])
    ran, _, _ = _hook_out(tmp_path, "--agent", name, AGENT_EVENTS[name][1])
    assert not ran


def test_a_codex_key_starts_no_python_for_claude_code(tmp_path):
    _home(tmp_path)
    (_home(tmp_path, agent_dir=".codex")
     / "credentials.json").write_text('{"api_key": "ri_x"}')
    assert not _hook(tmp_path, "PostToolUse")[0]


def test_codex_session_start_runs_until_its_install_notice_was_shown(tmp_path):
    _home(tmp_path)
    _home(tmp_path, notice_shown=False, agent_dir=".codex")
    assert _hook_out(tmp_path, "--agent", "codex", "SessionStart")[0]


def test_cursor_session_start_always_starts_python(tmp_path):
    # Its answer tells the /rius-* commands, /rius-login included, where
    # the plugin lives.
    _home(tmp_path, agent_dir=".cursor")
    assert _hook_out(tmp_path, "--agent", "cursor", "sessionStart")[0]


def test_an_unknown_agent_is_left_to_hook_py(tmp_path):
    _home(tmp_path)
    assert _hook_out(tmp_path, "--agent", "vim", "Stop")[0]


def test_the_off_path_is_fast(tmp_path):
    _home(tmp_path)
    timings = []
    for _ in range(5):
        ran, elapsed = _hook(tmp_path, "PreToolUse")
        assert not ran
        timings.append(elapsed)
    # SessionEnd's whole shared budget is 1.5 s; the off path is a shell
    # doing file tests, far inside it even on a slow CI runner.
    assert sorted(timings)[2] < 0.5, timings


def _with_credentials(tmp_path):
    (_home(tmp_path) / "credentials.json").write_text('{"api_key": "ri_x"}')
    return {}


def _with_an_open_trace(tmp_path):
    state = _home(tmp_path) / "state"
    state.mkdir()
    (state / "other-session.open").write_text("")
    return {}


def _with_no_home_at_all(tmp_path):
    _home(tmp_path)
    return {"HOME": "", "USERPROFILE": ""}


@pytest.mark.parametrize("setup", [
    _with_credentials, _with_an_open_trace,
    pytest.param(_with_no_home_at_all, marks=posix_only(
        "Git Bash sets HOME itself when the environment has none")),
])
@pytest.mark.parametrize("event", ("SessionStart", "PreToolUse", "SessionEnd"))
def test_anything_hook_py_could_act_on_starts_python(tmp_path, setup, event):
    ran, _ = _hook(tmp_path, event, **setup(tmp_path))
    assert ran


def test_session_start_runs_until_the_install_notice_was_shown(tmp_path):
    _home(tmp_path, notice_shown=False)
    assert _hook(tmp_path, "SessionStart")[0]


def test_session_start_runs_for_a_session_turned_on_before_sign_in(tmp_path):
    sessions = _home(tmp_path) / "sessions"
    sessions.mkdir()
    (sessions / "s1").write_text("on")
    assert _hook(tmp_path, "SessionStart")[0]


def test_session_start_runs_for_folders_enabled_before_sign_in(tmp_path):
    (_home(tmp_path) / "config.json").write_text('{"enabled_paths": ["/x"]}')
    assert _hook(tmp_path, "SessionStart")[0]


# --- hooks.json --------------------------------------------------------------

def _hook_entries():
    hooks = json.loads(HOOKS_JSON.read_text())["hooks"]
    for event, groups in hooks.items():
        for group in groups:
            for entry in group["hooks"]:
                yield event, entry


def test_every_hook_declares_a_timeout():
    for event, entry in _hook_entries():
        timeout = entry.get("timeout")
        assert isinstance(timeout, (int, float)) and 0 < timeout <= 10, (
            event, timeout)


def _agent_hook_entries(path):
    hooks = json.loads((ROOT / path).read_text())["hooks"]
    for event, groups in hooks.items():
        for group in groups:
            # Codex nests handlers like Claude Code; Cursor lists them flat.
            for entry in group.get("hooks", [group]):
                yield event, entry


@pytest.mark.parametrize("path", ["codex/hooks.json", "cursor/hooks.json"])
def test_every_codex_and_cursor_hook_declares_a_timeout(path):
    entries = list(_agent_hook_entries(path))
    assert entries
    for event, entry in entries:
        limit = 10 if event.lower() == "sessionstart" else 5
        assert entry.get("timeout") == limit, (path, event, entry)
        assert entry["command"].startswith('bash "${'), entry
        assert '/scripts/hook.sh" --agent ' in entry["command"], entry


def test_session_end_allows_a_cold_interpreter_start():
    """A SessionEnd hook that times out never closes the trace. A cold
    Python start on Windows can take over the default 1.5 s budget, and the
    off path skips Python entirely, so 5 s only matters when there is work."""
    ends = [entry["timeout"] for event, entry in _hook_entries()
            if event == "SessionEnd"]
    assert ends == [5], ends


# --- the macOS Command Line Tools stub ---------------------------------------

def _resolve_python(tmp_path, xcode_select_exit):
    bindir = tmp_path / "macbin"
    bindir.mkdir()
    for name, body in (("uname", "echo Darwin"),
                       ("xcode-select", "exit %d" % xcode_select_exit)):
        stub = bindir / name
        stub.write_text("#!/bin/sh\n%s\n" % body)
        stub.chmod(0o755)
    script = '. "%s"; printf "%%s" "$rius_py"' % FIND_PYTHON_SH
    r = subprocess.run([BASH, "-c", script], capture_output=True, text=True,
                       timeout=30, env={"PATH": "%s:/usr/bin:/bin" % bindir})
    return r.stdout


_needs_usr_bin_python3 = pytest.mark.skipif(
    not os.path.exists("/usr/bin/python3"), reason="no /usr/bin/python3 here")


@posix_only("/usr/bin/python3 is the macOS stub's path")
@_needs_usr_bin_python3
def test_the_stub_is_skipped_without_the_command_line_tools(tmp_path):
    assert _resolve_python(tmp_path, xcode_select_exit=2) == ""


@posix_only("/usr/bin/python3 is the macOS stub's path")
@_needs_usr_bin_python3
def test_usr_bin_python3_is_used_once_the_command_line_tools_exist(tmp_path):
    assert _resolve_python(tmp_path, xcode_select_exit=0) == "/usr/bin/python3"


# --- the interpreter runs isolated from the environment ----------------------

def _hostile_pythonpath(tmp_path):
    """A sitecustomize.py that a repo's settings.json env could put on
    PYTHONPATH, ahead of everything the plugin imports."""
    evil = tmp_path / "evil"
    evil.mkdir()
    marker = tmp_path / "sitecustomize-ran"
    (evil / "sitecustomize.py").write_text(
        "open(%r, 'w').close()\n" % str(marker))
    return str(evil), marker


def test_a_hostile_pythonpath_never_runs_inside_a_hook(tmp_path):
    evil, marker = _hostile_pythonpath(tmp_path)
    home = tmp_path / "home"
    (home / ".claude" / "rius").mkdir(parents=True)
    (home / ".claude" / "rius" / "credentials.json").write_text("{}")
    r = subprocess.run(
        [BASH, HOOK_SH, "SessionEnd"],
        input=json.dumps({"session_id": "s1", "cwd": str(tmp_path)}),
        capture_output=True, text=True, timeout=30,
        env=minimal_env(PATH=os.environ["PATH"], HOME=str(home),
                        PYTHONPATH=evil))
    assert r.returncode == 0, r.stderr
    assert not marker.exists()


def test_a_hostile_pythonpath_never_runs_inside_a_slash_command(tmp_path):
    evil, marker = _hostile_pythonpath(tmp_path)
    r = subprocess.run(
        [BASH, str(ROOT / "scripts" / "rius_ctl.sh"), "status", "--cwd", "/x"],
        capture_output=True, text=True, timeout=30,
        env=minimal_env(PATH=os.environ["PATH"], HOME=str(tmp_path),
                        PYTHONPATH=evil))
    assert "Rius tracing:" in r.stdout, r.stdout
    assert not marker.exists()


def test_detached_children_run_isolated_too():
    source = (ROOT / "scripts" / "hook.py").read_text()
    spawns = re.findall(r"\[sys\.executable, ([^,]+),", source)
    assert spawns == ['"-I"', '"-I"'], spawns
