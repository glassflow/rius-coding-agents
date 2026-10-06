"""`rius_ctl.sh use-key`: a console key, stored the way /rius:login stores
its own, with the endpoint taken from the built-in environment only."""
import os
import pathlib
import stat
import subprocess
import sys

import pytest

from rius_cc import agent, config, login
from tests.platforms import IS_WINDOWS
from tests.signed_in import sign_in

CTL = str(pathlib.Path(__file__).parent.parent / "scripts" / "rius_ctl.py")
KEY = "ri_" + "console1" + "." + "signature9"


def _use_key(home, args=(), stdin=KEY + "\n", env=None):
    e = {"HOME": home, "PATH": "/usr/bin:/bin"}
    e.update(env or {})
    return subprocess.run([sys.executable, CTL, "use-key"] + list(args),
                          input=stdin, capture_output=True, text=True, env=e,
                          timeout=30)


@pytest.fixture
def home(tmp_path):
    return str(tmp_path)


def test_the_key_is_stored_privately_with_the_production_endpoint(home):
    r = _use_key(home)
    assert "Stored the key for production" in r.stdout
    assert KEY not in r.stdout
    path = login.credentials_path(home)
    assert IS_WINDOWS or stat.S_IMODE(os.stat(path).st_mode) == 0o600
    c = config.resolve("s1", "/x", {}, home)
    assert (c.api_key, c.endpoint) == (KEY, "https://ingest.eu.console.rius-glassflow.com")
    assert c.key_source == "rius_ctl.sh use-key"


def test_staging_uses_the_staging_endpoint(home):
    _use_key(home, ["--env", "staging"])
    c = config.resolve("s1", "/x", {}, home)
    assert c.endpoint == "https://ingest.eu.staging.rius.glassflow.xyz"


def test_the_environment_cannot_choose_where_the_key_goes(home):
    _use_key(home, env={"RIUS_ENV": "staging",
                        "RIUS_ENDPOINT": "https://collector.attacker.example"})
    assert login.read_credentials(home)["endpoint"] == \
        "https://ingest.eu.console.rius-glassflow.com"
    c = config.resolve("s1", "/x", {}, home)
    assert (c.api_key, c.endpoint) == (KEY, "https://ingest.eu.console.rius-glassflow.com")


def test_an_unknown_environment_is_refused(home):
    r = _use_key(home, ["--env", "https://collector.attacker.example"])
    assert "--env takes production or staging" in r.stdout
    assert login.read_credentials(home) is None


@pytest.mark.parametrize("stdin", ["", "\n", "two words\n"])
def test_no_usable_key_writes_nothing(home, stdin):
    r = _use_key(home, stdin=stdin)
    assert "No key read" in r.stdout
    assert login.read_credentials(home) is None


def test_replacing_a_login_key_revokes_it(home):
    sign_in(home, api_key="glassflow_old")
    revoked = []
    login.use_key(home, KEY, "production",
                  post=lambda url, payload, bearer=None: revoked.append(bearer) or (200, {}))
    assert revoked == ["glassflow_old"]
    assert login.read_credentials(home)["api_key"] == KEY


def test_the_ignored_env_key_line_points_at_use_key():
    assert "rius_ctl.sh use-key" in config.IGNORED_API_KEY


@pytest.mark.parametrize("name", ["codex", "cursor"])
def test_a_console_key_is_stored_for_the_agent_named(home, name):
    r = _use_key(home, ["--agent", name])
    assert "Stored the key for production" in r.stdout
    with agent.using(agent.select(name)):
        assert config.resolve("s1", "/x", {}, home).api_key == KEY
    assert not os.path.exists(os.path.join(home, ".claude"))


@pytest.mark.parametrize("name", ["codex", "cursor"])
def test_the_no_key_hint_names_the_agent(home, name):
    r = _use_key(home, ["--agent", name], stdin="")
    assert "`pbpaste | rius_ctl.sh use-key --agent %s`" % name in r.stdout


def test_the_no_key_hint_for_claude_code_has_no_agent_flag(home):
    r = _use_key(home, stdin="")
    assert "`pbpaste | rius_ctl.sh use-key`" in r.stdout
