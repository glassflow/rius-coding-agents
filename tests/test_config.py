import json
import os

from rius_cc import config


def _home(tmp_path):
    os.makedirs(str(tmp_path / ".claude" / "rius"), exist_ok=True)
    return str(tmp_path)


BASE_ENV = {"RIUS_API_KEY": "glassflow_secret"}


def test_default_is_off(tmp_path):
    c = config.resolve("s1", "/x/y", BASE_ENV, _home(tmp_path))
    assert c.enabled is False
    assert "default" in c.reason


def test_missing_api_key_disables_even_when_enabled(tmp_path):
    home = _home(tmp_path)
    config.set_session_override("s1", home, True)
    c = config.resolve("s1", "/x/y", {}, home)
    assert c.enabled is False
    assert "RIUS_API_KEY" in c.reason


def test_session_override_beats_everything(tmp_path):
    home = _home(tmp_path)
    config.set_session_override("s1", home, True)
    env = dict(BASE_ENV, RIUS_CLAUDE_ENABLED="false")
    c = config.resolve("s1", "/x/y", env, home)
    assert c.enabled is True
    assert "session override" in c.reason


def test_session_override_can_disable(tmp_path):
    home = _home(tmp_path)
    config.set_session_override("s1", home, False)
    env = dict(BASE_ENV, RIUS_CLAUDE_ENABLED="true")
    assert config.resolve("s1", "/x/y", env, home).enabled is False


def test_clearing_session_override_falls_through(tmp_path):
    home = _home(tmp_path)
    config.set_session_override("s1", home, True)
    config.set_session_override("s1", home, None)
    env = dict(BASE_ENV, RIUS_CLAUDE_ENABLED="true")
    assert config.resolve("s1", "/x/y", env, home).enabled is True


def test_env_beats_path_rules(tmp_path):
    home = _home(tmp_path)
    with open(config.path_rules_path(home), "w") as fh:
        json.dump({"enabled_paths": ["/x/*"]}, fh)
    env = dict(BASE_ENV, RIUS_CLAUDE_ENABLED="false")
    c = config.resolve("s1", "/x/y", env, home)
    assert c.enabled is False


def test_path_rule_enables_folder_and_subfolders(tmp_path):
    home = _home(tmp_path)
    with open(config.path_rules_path(home), "w") as fh:
        json.dump({"enabled_paths": ["/opt/glass0"]}, fh)
    assert config.resolve("s1", "/opt/glass0", BASE_ENV, home).enabled is True
    assert config.resolve("s1", "/opt/glass0/deep/dir", BASE_ENV, home).enabled is True
    assert config.resolve("s1", "/opt/other", BASE_ENV, home).enabled is False


def test_disabled_path_beats_enabled_path(tmp_path):
    home = _home(tmp_path)
    with open(config.path_rules_path(home), "w") as fh:
        json.dump({"enabled_paths": ["/opt/*"],
                   "disabled_paths": ["/opt/secret"]}, fh)
    assert config.resolve("s1", "/opt/ok", BASE_ENV, home).enabled is True
    assert config.resolve("s1", "/opt/secret", BASE_ENV, home).enabled is False


def test_corrupt_path_rules_do_not_raise(tmp_path):
    home = _home(tmp_path)
    with open(config.path_rules_path(home), "w") as fh:
        fh.write("{not json")
    c = config.resolve("s1", "/x/y", BASE_ENV, home)
    assert c.enabled is False


def test_defaults_and_overrides(tmp_path):
    home = _home(tmp_path)
    c = config.resolve("s1", "/x", BASE_ENV, home)
    assert c.endpoint == "https://ingest.eu.console.rius-glassflow.com"
    assert c.service_name == "claude-code"
    assert c.capture_content is True
    assert c.max_attr_bytes == 32768
    env = dict(BASE_ENV, RIUS_ENDPOINT="https://ingest.staging.rius.glassflow.xyz",
               RIUS_SERVICE_NAME="cc-dev", RIUS_CAPTURE_CONTENT="false",
               RIUS_CLAUDE_MAX_ATTR_BYTES="100")
    c = config.resolve("s1", "/x", env, home)
    assert c.endpoint == "https://ingest.staging.rius.glassflow.xyz"
    assert c.service_name == "cc-dev"
    assert c.capture_content is False
    assert c.max_attr_bytes == 100


def test_redact_never_leaks_the_key():
    assert config.redact("glassflow_abcdef123456") == "glassflow_…"
    assert "abcdef" not in config.redact("glassflow_abcdef123456")
    assert config.redact(None) == "<unset>"
