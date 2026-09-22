import json
import subprocess
import sys
import pathlib

CTL = str(pathlib.Path(__file__).parent.parent / "scripts" / "rius_ctl.py")


def _run(args, home, env=None):
    e = {"HOME": home, "PATH": "/usr/bin:/bin"}
    e.update(env or {})
    return subprocess.run([sys.executable, CTL] + args,
                          capture_output=True, text=True, env=e, timeout=30)


def test_status_explains_why_it_is_off(tmp_path):
    home = str(tmp_path)
    (tmp_path / ".claude" / "rius").mkdir(parents=True)
    r = _run(["status", "--session", "s1", "--cwd", "/x/y"], home,
             {"RIUS_API_KEY": "glassflow_k"})
    assert r.returncode == 0
    assert "off" in r.stdout.lower()
    assert "/x/y" in r.stdout or "default" in r.stdout.lower()


def test_status_never_prints_the_key(tmp_path):
    home = str(tmp_path)
    (tmp_path / ".claude" / "rius").mkdir(parents=True)
    r = _run(["status", "--session", "s1", "--cwd", "/x"], home,
             {"RIUS_API_KEY": "glassflow_supersecret"})
    assert "supersecret" not in r.stdout


def test_on_then_status_reports_on(tmp_path):
    home = str(tmp_path)
    (tmp_path / ".claude" / "rius").mkdir(parents=True)
    _run(["on", "--session", "s1", "--cwd", "/x"], home, {"RIUS_API_KEY": "glassflow_k"})
    r = _run(["status", "--session", "s1", "--cwd", "/x"], home, {"RIUS_API_KEY": "glassflow_k"})
    assert "on" in r.stdout.lower()


def test_enable_here_adds_a_path_rule(tmp_path):
    home = str(tmp_path)
    (tmp_path / ".claude" / "rius").mkdir(parents=True)
    _run(["enable-here", "--session", "s1", "--cwd", "/opt/proj"], home,
         {"RIUS_API_KEY": "glassflow_k"})
    rules = json.load(open(str(tmp_path / ".claude" / "rius" / "config.json")))
    assert "/opt/proj" in rules["enabled_paths"]


def test_enable_here_is_idempotent(tmp_path):
    home = str(tmp_path)
    (tmp_path / ".claude" / "rius").mkdir(parents=True)
    for _ in range(3):
        _run(["enable-here", "--session", "s1", "--cwd", "/opt/proj"], home,
             {"RIUS_API_KEY": "glassflow_k"})
    rules = json.load(open(str(tmp_path / ".claude" / "rius" / "config.json")))
    assert rules["enabled_paths"].count("/opt/proj") == 1


# --- C4: the real /rius path passes no --session at all ------------------

def _fresh_home(tmp_path):
    home = str(tmp_path)
    (tmp_path / ".claude" / "rius").mkdir(parents=True)
    return home


def test_on_without_session_refuses_instead_of_guessing(tmp_path):
    """commands/rius.md never passes --session. On a fresh install there is
    no state file, so the guessed session id was "" -- which made the
    override path the sessions DIRECTORY and open(dir, "w") raise
    IsADirectoryError. /rius on simply did not work on a fresh install."""
    home = _fresh_home(tmp_path)
    r = _run(["on"], home, {"RIUS_API_KEY": "glassflow_k"})
    assert r.returncode == 0
    lower = r.stdout.lower()
    assert "isadirectory" not in lower and "error:" not in lower
    assert "session" in lower
    assert "enable-here" in r.stdout          # points at the thing that works
    assert not (tmp_path / ".claude" / "rius" / "sessions").exists()


def test_off_and_clear_without_session_also_refuse(tmp_path):
    home = _fresh_home(tmp_path)
    for action in ("off", "clear"):
        r = _run([action], home, {"RIUS_API_KEY": "glassflow_k"})
        assert r.returncode == 0
        assert "enable-here" in r.stdout, action
        assert "isadirectory" not in r.stdout.lower(), action


def test_on_without_session_never_targets_another_live_session(tmp_path):
    """Two concurrent sessions: the override used to land on whichever one
    wrote state most recently, silently enabling somebody else's session."""
    home = _fresh_home(tmp_path)
    import sys as _sys, pathlib as _pathlib
    _sys.path.insert(0, str(_pathlib.Path(__file__).parent.parent / "scripts"))
    from rius_cc import state as _state
    _state.save("other-session", home, _state.new_state())

    r = _run(["on"], home, {"RIUS_API_KEY": "glassflow_k"})
    assert r.returncode == 0
    assert "enable-here" in r.stdout
    assert not (tmp_path / ".claude" / "rius" / "sessions" / "other-session").exists()


def test_on_with_an_empty_session_argument_refuses(tmp_path):
    home = _fresh_home(tmp_path)
    r = _run(["on", "--session", ""], home, {"RIUS_API_KEY": "glassflow_k"})
    assert r.returncode == 0
    assert "enable-here" in r.stdout
    assert not (tmp_path / ".claude" / "rius" / "sessions").exists()


def test_status_without_session_says_it_inferred_one(tmp_path):
    """status keeps the fallback -- it only reads -- but must say so."""
    home = _fresh_home(tmp_path)
    import sys as _sys, pathlib as _pathlib
    _sys.path.insert(0, str(_pathlib.Path(__file__).parent.parent / "scripts"))
    from rius_cc import state as _state
    st = _state.new_state()
    st["spans_exported"] = 7
    _state.save("guessed-session", home, st)

    r = _run(["status", "--cwd", "/x"], home, {"RIUS_API_KEY": "glassflow_k"})
    assert r.returncode == 0
    assert "guessed-session" in r.stdout
    assert "inferred" in r.stdout.lower()
    assert "7" in r.stdout


def test_status_on_a_fresh_install_does_not_crash(tmp_path):
    home = _fresh_home(tmp_path)
    r = _run(["status", "--cwd", "/x"], home, {"RIUS_API_KEY": "glassflow_k"})
    assert r.returncode == 0
    assert "unknown" in r.stdout.lower()
    assert "error" not in r.stdout.lower()


def test_status_with_an_explicit_session_does_not_claim_to_infer(tmp_path):
    home = _fresh_home(tmp_path)
    r = _run(["status", "--session", "s1", "--cwd", "/x"], home,
             {"RIUS_API_KEY": "glassflow_k"})
    assert "inferred" not in r.stdout.lower()


def test_enable_here_still_needs_no_session(tmp_path):
    home = _fresh_home(tmp_path)
    r = _run(["enable-here", "--cwd", "/opt/proj"], home,
             {"RIUS_API_KEY": "glassflow_k"})
    assert r.returncode == 0
    rules = json.load(open(str(tmp_path / ".claude" / "rius" / "config.json")))
    assert "/opt/proj" in rules["enabled_paths"]


def test_unknown_action_exits_zero_with_usage(tmp_path):
    home = str(tmp_path)
    (tmp_path / ".claude" / "rius").mkdir(parents=True)
    r = _run(["wat", "--session", "s1", "--cwd", "/x"], home)
    assert r.returncode == 0
    assert "usage" in r.stdout.lower()


# --- TRAP 4, second entry point: how /rius is invoked at all ----------------
#
# `/rius status` is the ONLY surface that tells a user why tracing is off, so
# a /rius that cannot run on Windows is the worst thing to lose there.

import os      # noqa: E402
import re      # noqa: E402

ROOT = pathlib.Path(__file__).parent.parent
COMMAND_MD = ROOT / "commands" / "rius.md"
CTL_SH = str(ROOT / "scripts" / "rius_ctl.sh")


def _command_line():
    """The one `!`...`` bash substitution Claude Code runs for /rius."""
    m = re.search(r"^!`(.+)`\s*$", COMMAND_MD.read_text(), re.M)
    assert m, "commands/rius.md has no bash substitution line"
    return m.group(1)


def test_slash_command_does_not_rely_on_the_shebang():
    """Windows has no shebang support, so a command file that executes
    rius_ctl.py directly is a /rius that does nothing there -- the same
    mechanism that broke the hook, on the one command that would have
    explained it."""
    line = _command_line()
    assert "rius_ctl.sh" in line, line
    assert not re.search(r"rius_ctl\.py", line), \
        "/rius still invokes the .py directly and relies on its shebang"


def test_slash_command_target_exists_on_disk():
    line = _command_line()
    named = re.findall(r"\$\{CLAUDE_PLUGIN_ROOT\}(/[\w./-]+)", line)
    assert named, line
    for rel in named:
        assert (ROOT / rel.lstrip("/")).exists(), rel


def test_slash_command_is_permitted_by_its_own_frontmatter():
    """allowed-tools still naming the .py would make the launcher prompt."""
    front = COMMAND_MD.read_text().split("---")[1]
    assert "rius_ctl.sh" in front, front


def test_slash_command_line_runs_end_to_end(tmp_path):
    """Mirrors test_launcher_runs_the_hook_end_to_end: run the literal line
    from the command file, not an approximation of it."""
    home = tmp_path / "home"
    (home / ".claude" / "rius").mkdir(parents=True)
    env = {"HOME": str(home), "PATH": os.environ["PATH"],
           "CLAUDE_PLUGIN_ROOT": str(ROOT), "RIUS_API_KEY": "glassflow_k"}
    r = subprocess.run(["/bin/bash", "-c", _command_line()],
                       cwd=str(tmp_path), capture_output=True, text=True,
                       env=env, timeout=30)
    assert r.returncode == 0, r.stderr
    assert "off" in r.stdout.lower()
    assert "glassflow_k" not in r.stdout


def test_ctl_launcher_says_so_when_no_python_can_be_found(tmp_path):
    """Unlike hook.sh this one MAY write to stdout -- its stdout is the
    slash command's output -- so the failure that hook.sh can only put in a
    log file goes where the user is already looking."""
    home = tmp_path / "home"
    (home / ".claude" / "rius").mkdir(parents=True)
    fake = tmp_path / "fakebin"
    fake.mkdir()
    for name in ("python", "python3"):
        stub = fake / name
        stub.write_text("#!/bin/sh\nexit 1\n")
        stub.chmod(0o755)
    env = {"HOME": str(home), "PATH": str(fake)}
    r = subprocess.run(["/bin/bash", CTL_SH, "status", "--cwd", "/x"],
                       capture_output=True, text=True, env=env, timeout=30)
    assert r.returncode == 0
    assert "python" in r.stdout.lower()
