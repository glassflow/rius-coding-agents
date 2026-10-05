"""RIUS-1224: a repository's settings.json can set PATH for every hook, so
_find_python.sh never runs an interpreter from inside the project folder,
and honours an interpreter pinned in ~/.claude/rius/python."""
import os
import pathlib
import subprocess

import pytest

from tests.platforms import (BASH, PYTHON_FOR_SH, TOOLS_WITHOUT_PYTHON,
                             minimal_env, posix_only)

FIND_PYTHON_SH = str(pathlib.Path(__file__).parent.parent / "scripts"
                     / "_find_python.sh")


def _python_in(folder):
    folder.mkdir(parents=True, exist_ok=True)
    stub = folder / "python3"
    stub.write_text('#!/bin/sh\nexec "%s" "$@"\n' % PYTHON_FOR_SH)
    stub.chmod(0o755)
    return stub


def _bash_path(path):
    """How Git Bash spells a Windows path; unchanged on POSIX."""
    text = str(path).replace("\\", "/")
    if len(text) > 1 and text[1] == ":":
        text = "/" + text[0].lower() + text[2:]
    return text


@pytest.fixture
def world(tmp_path):
    home = tmp_path / "home"
    (home / ".claude" / "rius").mkdir(parents=True)
    project = tmp_path / "project"
    project.mkdir()
    fallback = _python_in(tmp_path / "system" / "bin")
    return home, project, fallback


def _resolve(world, path_entries, cwd=None, **env):
    home, project, fallback = world
    entries = [str(e) for e in path_entries] + [str(fallback.parent)]
    if TOOLS_WITHOUT_PYTHON:
        entries.append(TOOLS_WITHOUT_PYTHON)
    script = '. "%s"; printf "%%s" "$rius_py"' % _bash_path(FIND_PYTHON_SH)
    r = subprocess.run([BASH, "-c", script], cwd=str(cwd or project),
                       capture_output=True, text=True, timeout=30,
                       env=minimal_env(PATH=os.pathsep.join(entries),
                                       HOME=str(home), **env))
    assert r.returncode == 0, r.stderr
    return r.stdout


def _is(found, stub):
    return found == _bash_path(stub)


def test_a_python_on_a_normal_path_outside_the_project_is_used(world, tmp_path):
    pyenv = _python_in(tmp_path / "pyenv" / "shims")
    assert _is(_resolve(world, [pyenv.parent]), pyenv)


def test_a_python_in_a_path_entry_inside_the_project_is_skipped(world):
    _home, project, fallback = world
    planted = _python_in(project / "node_modules" / ".bin")
    assert _is(_resolve(world, [planted.parent]), fallback)


@posix_only("Git Bash rewrites relative PATH entries when it starts")
def test_a_python_in_a_relative_path_entry_is_skipped(world):
    _home, project, fallback = world
    _python_in(project / "bin")
    assert _is(_resolve(world, ["bin"]), fallback)


@posix_only("symlinks need extra privileges on Windows")
def test_a_symlink_from_outside_into_the_project_is_skipped(world, tmp_path):
    _home, project, fallback = world
    planted = _python_in(project / ".tools")
    outside = tmp_path / "looks-normal"
    outside.mkdir()
    os.symlink(str(planted), str(outside / "python3"))
    assert _is(_resolve(world, [outside]), fallback)


def test_claude_project_dir_is_the_project_even_from_another_cwd(world, tmp_path):
    _home, project, fallback = world
    planted = _python_in(project / "bin")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    assert _is(_resolve(world, [planted.parent], cwd=elsewhere,
                        CLAUDE_PROJECT_DIR=str(project)), fallback)


def test_a_session_started_in_home_still_finds_pythons_under_home(world):
    home, _project, _fallback = world
    pyenv = _python_in(home / ".pyenv" / "shims")
    assert _is(_resolve(world, [pyenv.parent], cwd=home), pyenv)


def _pin(home, interpreter):
    (home / ".claude" / "rius" / "python").write_text(str(interpreter) + "\n")


def test_a_pinned_interpreter_is_used_first(world, tmp_path):
    home, _project, _fallback = world
    pinned = _python_in(tmp_path / "opt" / "python" / "bin")
    on_path = _python_in(tmp_path / "pyenv" / "shims")
    _pin(home, pinned)
    found = _resolve(world, [on_path.parent])
    assert found.replace("\\", "/") in (str(pinned).replace("\\", "/"),
                                        _bash_path(pinned)), found


def test_a_pin_inside_the_project_is_ignored(world):
    home, project, fallback = world
    _pin(home, _python_in(project / "venv" / "bin"))
    assert _is(_resolve(world, []), fallback)


def test_a_relative_or_missing_pin_falls_back_to_path(world):
    home, _project, fallback = world
    _pin(home, "python3")
    assert _is(_resolve(world, []), fallback)
    _pin(home, "/no/such/python3")
    assert _is(_resolve(world, []), fallback)
