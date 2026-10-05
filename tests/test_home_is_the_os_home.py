"""A cloned repo's env block can set HOME or USERPROFILE, pointing the
plugin at a folder of its own with its own key and path rules. The home
comes from the operating system instead."""
import os
import pathlib
import shutil
import subprocess
import sys

import pytest

from rius_cc import platform_compat as pc
from tests.platforms import IS_WINDOWS

ROOT = pathlib.Path(__file__).parent.parent
MARKER = "_tests_trust_env_home.py"


def _shipped_copy(tmp_path):
    """scripts/ as it ships: without the suite's marker."""
    copy = tmp_path / "scripts"
    shutil.copytree(str(ROOT / "scripts"), str(copy),
                    ignore=shutil.ignore_patterns(MARKER, "__pycache__"))
    return copy


def _os_home():
    return pc._windows_home() if IS_WINDOWS else pc._posix_home()


def test_a_hostile_home_is_ignored(tmp_path):
    copy = _shipped_copy(tmp_path)
    hostile = str(tmp_path / "repo" / ".claude" / "fake")
    env = dict(os.environ, HOME=hostile, USERPROFILE=hostile)
    r = subprocess.run(
        [sys.executable, "-c", "import sys; sys.path.insert(0, sys.argv[1]); "
         "from rius_cc import platform_compat as pc; print(pc.home_dir())",
         str(copy)], capture_output=True, text=True, env=env, timeout=30)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == _os_home()
    assert r.stdout.strip() != hostile


def test_a_relative_home_is_ignored_too(tmp_path):
    copy = _shipped_copy(tmp_path)
    env = dict(os.environ, HOME=".claude/fake", USERPROFILE=".claude/fake")
    r = subprocess.run(
        [sys.executable, "-c", "import sys; sys.path.insert(0, sys.argv[1]); "
         "from rius_cc import platform_compat as pc; print(pc.home_dir())",
         str(copy)], capture_output=True, text=True, env=env, timeout=30)
    assert r.stdout.strip() == _os_home()


def test_the_os_home_is_an_existing_absolute_folder():
    home = _os_home()
    assert os.path.isabs(home) and os.path.isdir(home)


def test_the_marker_is_never_committed():
    tracked = subprocess.run(["git", "ls-files"], cwd=str(ROOT),
                             capture_output=True, text=True).stdout.split()
    assert not [f for f in tracked if f.endswith(MARKER)]


@pytest.mark.parametrize("script", ["hook.py", "exporter.py", "rius_ctl.py"])
def test_every_entry_point_uses_the_one_resolver(script):
    source = (ROOT / "scripts" / script).read_text()
    assert "platform_compat.home_dir(" in source
    assert "expanduser" not in source and 'environ["HOME"]' not in source


def test_the_test_marker_counts_only_in_a_git_checkout(monkeypatch, tmp_path):
    from rius_cc import platform_compat as pc
    assert pc._tests_trust_env_home()
    monkeypatch.setattr(pc, "_plugin_root", lambda: str(tmp_path))
    assert not pc._tests_trust_env_home()
