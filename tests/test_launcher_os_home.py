"""The shell launchers find the home folder from the OS, never $HOME.

A cloned repo's .claude/settings.json can set HOME and USERPROFILE for every
hook. hook.sh's fast path and _find_python.sh's pin and project rule must
not follow them. The suite's own marker module (conftest) is what lets the
other tests choose HOME, so these tests run the launchers from a copy of
scripts/ that has no marker, just as the plugin ships.
"""
import glob
import json
import os
import pathlib
import shutil
import subprocess

from rius_cc import platform_compat
from tests.platforms import (BASH, IS_WINDOWS, PYTHON_FOR_SH,
                             TOOLS_WITHOUT_PYTHON, minimal_env, posix_only)

SCRIPTS = pathlib.Path(__file__).parent.parent / "scripts"


def _bash_path(path):
    text = str(path).replace("\\", "/")
    if len(text) > 1 and text[1] == ":":
        text = "/" + text[0].lower() + text[2:]
    return text


def _shipped_scripts(tmp_path):
    """scripts/ as installed: the launchers, no test marker."""
    bare = tmp_path / "scripts"
    bare.mkdir()
    for name in ("hook.sh", "_find_python.sh", "_os_home.sh"):
        shutil.copy(str(SCRIPTS / name), str(bare / name))
    return bare


def _real_home():
    if IS_WINDOWS:
        return platform_compat._windows_home()
    return platform_compat._posix_home()


def _hostile_home(tmp_path):
    """What a repo would point HOME at: a key and an open trace of its own."""
    home = tmp_path / "hostile"
    rius = home / ".claude" / "rius"
    (rius / "state").mkdir(parents=True)
    (rius / "credentials.json").write_text("{}")
    (rius / "state" / "s.open").write_text("")
    return home


def _recording_python(tmp_path):
    bindir = tmp_path / "fakebin"
    bindir.mkdir(exist_ok=True)
    log = tmp_path / "python-started"
    for name in ("python3", "python", "py"):
        stub = bindir / name
        stub.write_text('#!/bin/sh\necho "$0" >> "%s"\nexit 0\n'
                        % _bash_path(log))
        stub.chmod(0o755)
    return bindir, log


def _path(*entries):
    parts = [str(e) for e in entries]
    if TOOLS_WITHOUT_PYTHON:
        parts.append(TOOLS_WITHOUT_PYTHON)
    return os.pathsep.join(parts)


def _source(bare, name, then, cwd, **env):
    script = 'dir="%s"; . "$dir/%s"; %s' % (_bash_path(bare), name, then)
    r = subprocess.run([BASH, "-c", script], cwd=str(cwd), capture_output=True,
                       text=True, timeout=30, env=minimal_env(**env))
    assert r.returncode == 0, r.stderr
    return r.stdout


def test_the_os_home_ignores_home_and_userprofile(tmp_path):
    bare = _shipped_scripts(tmp_path)
    hostile = _hostile_home(tmp_path)
    out = _source(bare, "_os_home.sh", 'printf "%s" "$rius_os_home"', tmp_path,
                  PATH=_path(), HOME=str(hostile), USERPROFILE=str(hostile))
    assert out == _bash_path(_real_home())


def _real_home_has_a_key_or_open_trace():
    rius = os.path.join(_real_home(), ".claude", "rius")
    return (os.path.exists(os.path.join(rius, "credentials.json"))
            or bool(glob.glob(os.path.join(rius, "state", "*.open"))))


def _run_hook(bare, tmp_path, path, **env):
    r = subprocess.run([BASH, str(bare / "hook.sh"), "PreToolUse"],
                       input=json.dumps({"session_id": "s1", "cwd": "/x"}),
                       capture_output=True, text=True, timeout=30,
                       env=minimal_env(PATH=path, **env))
    assert r.returncode == 0, r.stderr
    return (tmp_path / "python-started").exists()


def test_a_key_under_a_hostile_home_does_not_wake_python(tmp_path):
    bare = _shipped_scripts(tmp_path)
    hostile = _hostile_home(tmp_path)
    bindir, _ = _recording_python(tmp_path)
    ran = _run_hook(bare, tmp_path, _path(bindir), HOME=str(hostile),
                    USERPROFILE=str(hostile))
    assert ran == _real_home_has_a_key_or_open_trace()


def _tools_without_id(tmp_path):
    tools = tmp_path / "tools"
    tools.mkdir()
    for tool in ("uname", "tr", "realpath", "readlink", "mkdir", "date",
                 "cat", "xcode-select"):
        found = shutil.which(tool)
        if found:
            os.symlink(found, str(tools / tool))
    return tools


@posix_only("Git Bash resolves the profile with cygpath, not id")
def test_a_home_that_cannot_be_resolved_runs_python(tmp_path):
    bare = _shipped_scripts(tmp_path)
    bindir, _ = _recording_python(tmp_path)
    empty = tmp_path / "empty-home"
    empty.mkdir()
    path = os.pathsep.join((str(bindir), str(_tools_without_id(tmp_path))))
    assert _run_hook(bare, tmp_path, path, HOME=str(empty))


def _python_in(folder):
    folder.mkdir(parents=True, exist_ok=True)
    stub = folder / "python3"
    stub.write_text('#!/bin/sh\nexec "%s" "$@"\n' % PYTHON_FOR_SH)
    stub.chmod(0o755)
    return stub


def _find_python(bare, cwd, path, **env):
    return _source(bare, "_find_python.sh", 'printf "%s" "$rius_py"', cwd,
                   PATH=path, **env)


@posix_only("the pin and project rule tests in test_find_python cover Windows")
def test_a_pin_under_a_hostile_home_is_not_read(tmp_path):
    bare = _shipped_scripts(tmp_path)
    hostile = _hostile_home(tmp_path)
    pinned = _python_in(tmp_path / "pinned" / "bin")
    (hostile / ".claude" / "rius" / "python").write_text(str(pinned) + "\n")
    fallback = _python_in(tmp_path / "system" / "bin")
    project = tmp_path / "project"
    project.mkdir()
    found = _find_python(bare, project, _path(fallback.parent),
                         HOME=str(hostile))
    assert os.path.realpath(found) == os.path.realpath(str(fallback))


@posix_only("the pin and project rule tests in test_find_python cover Windows")
def test_home_set_to_the_project_keeps_the_project_rule_on(tmp_path):
    bare = _shipped_scripts(tmp_path)
    project = tmp_path / "project"
    planted = _python_in(project / "bin")
    fallback = _python_in(tmp_path / "system" / "bin")
    found = _find_python(bare, project, _path(planted.parent, fallback.parent),
                         HOME=str(project), CLAUDE_PROJECT_DIR=str(project))
    assert os.path.realpath(found) == os.path.realpath(str(fallback))
