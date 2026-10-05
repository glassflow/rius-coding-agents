"""Shared fixtures. `pythonpath = ["scripts"]` in pyproject makes rius_cc importable."""
import atexit
import os
import pathlib
import re

import pytest

# The plugin ignores $HOME (a cloned repo could set it), except when this
# module exists in a git checkout. Only the suite creates it, so every test, and every process
# a test starts, can be given its own HOME. It is never committed.
TRUST_ENV_HOME = (pathlib.Path(__file__).parent.parent / "scripts" / "rius_cc"
                  / "_tests_trust_env_home.py")
TRUST_ENV_HOME.write_text('"""Test-only: lets $HOME choose the home. Never shipped."""\n')


def _remove_trust_marker():
    if TRUST_ENV_HOME.exists():
        TRUST_ENV_HOME.unlink()


atexit.register(_remove_trust_marker)


def pytest_sessionfinish(session, exitstatus):
    """Before atexit, which a killed or crashed run may never reach."""
    _remove_trust_marker()

# The developer's REAL state directory, resolved before any test can
# override HOME. No test may write there.
REAL_STATE_DIR = os.path.join(os.path.expanduser("~"), ".claude", "rius",
                              "state")

# Real Claude Code session ids are uuid4s. Test ids never are ("s1",
# "11111111-1111-...", "aaaaaaaa-0000-..."), so a new file that does not
# start with one is a test leaking into the real directory; files of the
# developer's live sessions, which may appear while the suite runs, are not.
_REAL_SESSION = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")


def leaked(before, after):
    """Names that appeared in the real state dir and are not a live session's."""
    return sorted(n for n in set(after) - set(before)
                  if not _REAL_SESSION.match(n))


def _listing(path):
    try:
        return os.listdir(path)
    except OSError:
        return []


@pytest.fixture(scope="session", autouse=True)
def real_state_dir_is_untouched():
    before = _listing(REAL_STATE_DIR)
    yield
    new = leaked(before, _listing(REAL_STATE_DIR))
    if new:
        pytest.fail("the test suite wrote into the real %s: %s -- a test ran "
                    "without an isolated HOME" % (REAL_STATE_DIR, new),
                    pytrace=False)


# Codex's and Cursor's Rius directories: always under the OS home, whatever
# CODEX_HOME says, and no test may create or change anything under them.
REAL_AGENT_RIUS_DIRS = [os.path.join(os.path.expanduser("~"), agent_dir, "rius")
                        for agent_dir in (".codex", ".cursor")]


def _tree(path):
    found = []
    for root, _, files in os.walk(path):
        found += [(os.path.join(root, f), os.path.getmtime(os.path.join(root, f)))
                  for f in files]
    return sorted(found)


@pytest.fixture(scope="session", autouse=True)
def real_agent_dirs_are_untouched():
    before = [_tree(path) for path in REAL_AGENT_RIUS_DIRS]
    yield
    for path, tree in zip(REAL_AGENT_RIUS_DIRS, before):
        if _tree(path) != tree:
            pytest.fail("the test suite wrote into the real %s -- a test ran "
                        "without an isolated HOME" % path, pytrace=False)


@pytest.fixture
def fixtures_dir() -> pathlib.Path:
    return pathlib.Path(__file__).parent / "fixtures"
