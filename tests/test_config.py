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
        json.dump({"enabled_paths": ["/opt/example"]}, fh)
    assert config.resolve("s1", "/opt/example", BASE_ENV, home).enabled is True
    assert config.resolve("s1", "/opt/example/deep/dir", BASE_ENV, home).enabled is True
    assert config.resolve("s1", "/opt/other", BASE_ENV, home).enabled is False


def test_disabled_path_beats_enabled_path(tmp_path):
    home = _home(tmp_path)
    with open(config.path_rules_path(home), "w") as fh:
        json.dump({"enabled_paths": ["/opt/*"],
                   "disabled_paths": ["/opt/secret"]}, fh)
    assert config.resolve("s1", "/opt/ok", BASE_ENV, home).enabled is True
    assert config.resolve("s1", "/opt/secret", BASE_ENV, home).enabled is False


def test_degenerate_enabled_path_rules_never_match_anything(tmp_path):
    """SECURITY, fail-open. `cwd.startswith(rule.rstrip("/") + "/")` is true
    for EVERY absolute path when rule is "" -- and fnmatch("*") matches
    everything. A hand-edited or truncated config.json with a stray entry in
    enabled_paths would silently trace every folder on the machine, with
    full content capture."""
    home = _home(tmp_path)
    for bad in ["", "/", " ", "//", "*", "**", "opt/example", "?"]:
        with open(config.path_rules_path(home), "w") as fh:
            json.dump({"enabled_paths": [bad]}, fh)
        c = config.resolve("s1", "/opt/example/anything", BASE_ENV, home)
        assert c.enabled is False, "rule %r enabled an unrelated folder" % (bad,)
        assert c.enabled is False
        c = config.resolve("s1", "/", BASE_ENV, home)
        assert c.enabled is False, "rule %r enabled the filesystem root" % (bad,)


def test_degenerate_disabled_path_rules_are_ignored_too(tmp_path):
    """A stray "" in disabled_paths fails closed rather than open, but it
    still silently kills tracing everywhere. Same rule: skip it."""
    home = _home(tmp_path)
    with open(config.path_rules_path(home), "w") as fh:
        json.dump({"enabled_paths": ["/opt/example"], "disabled_paths": [""]}, fh)
    assert config.resolve("s1", "/opt/example/x", BASE_ENV, home).enabled is True


def test_real_path_rules_still_match(tmp_path):
    """The skip must not eat legitimate rules, including globs."""
    home = _home(tmp_path)
    with open(config.path_rules_path(home), "w") as fh:
        json.dump({"enabled_paths": ["/opt/example", "/srv/*/checkout"]}, fh)
    assert config.resolve("s1", "/opt/example", BASE_ENV, home).enabled is True
    assert config.resolve("s1", "/opt/example/deep", BASE_ENV, home).enabled is True
    assert config.resolve("s1", "/srv/a/checkout", BASE_ENV, home).enabled is True
    assert config.resolve("s1", "/srv/a/other", BASE_ENV, home).enabled is False


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


def test_redact_ri_form():
    assert config.redact("ri_S8QkXkns.OtpIDoWm") == "ri_…"
    assert "S8QkXk" not in config.redact("ri_S8QkXkns.OtpIDoWm")
    assert "OtpIDo" not in config.redact("ri_S8QkXkns.OtpIDoWm")


def test_redact_no_underscore():
    assert config.redact("nounderscorehere") == "<redacted>"
    assert "nounde" not in config.redact("nounderscorehere")


def test_missing_api_key_mentioned_even_when_default_off(tmp_path):
    home = _home(tmp_path)
    c = config.resolve("s1", "/x/y", {}, home)
    assert c.enabled is False
    assert "RIUS_API_KEY" in c.reason


# --- Windows path rules -----------------------------------------------------
#
# `/rius enable-here` writes the cwd verbatim into enabled_paths. On Windows
# that is `C:\proj`, which the POSIX-only leading-slash check rejected as
# unusable -- so enable-here reported success, tracing stayed off, and
# nothing anywhere said why.

import pytest  # noqa: E402


@pytest.fixture
def as_windows(monkeypatch):
    monkeypatch.setattr(config, "IS_WINDOWS", True)


def test_windows_drive_path_rule_enables_folder_and_subfolders(tmp_path, as_windows):
    home = _home(tmp_path)
    with open(config.path_rules_path(home), "w") as fh:
        json.dump({"enabled_paths": ["C:\\Users\\kiran\\proj"]}, fh)
    assert config.resolve("s1", "C:\\Users\\kiran\\proj",
                          BASE_ENV, home).enabled is True
    assert config.resolve("s1", "C:\\Users\\kiran\\proj\\src",
                          BASE_ENV, home).enabled is True
    assert config.resolve("s1", "C:\\Users\\kiran\\other",
                          BASE_ENV, home).enabled is False


def test_windows_matching_ignores_case_and_separator(tmp_path, as_windows):
    """The Windows filesystem treats these as one path, so a rule the user
    just wrote must not fail to match its own directory."""
    home = _home(tmp_path)
    with open(config.path_rules_path(home), "w") as fh:
        json.dump({"enabled_paths": ["C:\\Users\\Kiran\\Proj"]}, fh)
    for spelling in ("c:/users/kiran/proj",
                     "C:/Users/Kiran/Proj/src",
                     "c:\\USERS\\kiran\\PROJ\\deep"):
        assert config.resolve("s1", spelling, BASE_ENV, home).enabled is True, spelling


def test_windows_unc_path_rule_works(tmp_path, as_windows):
    home = _home(tmp_path)
    with open(config.path_rules_path(home), "w") as fh:
        json.dump({"enabled_paths": ["\\\\build\\share\\proj"]}, fh)
    assert config.resolve("s1", "\\\\build\\share\\proj\\src",
                          BASE_ENV, home).enabled is True
    assert config.resolve("s1", "\\\\build\\other\\proj",
                          BASE_ENV, home).enabled is False


def test_degenerate_windows_rules_never_match_anything(tmp_path, as_windows):
    """SECURITY, same rule as the POSIX case: a bare drive root is every
    path on that drive, and a server with no share is not a location."""
    home = _home(tmp_path)
    for bad in ["C:\\", "C:/", "c:\\", "C:", "\\\\", "\\\\srv", "//",
                "", " ", "*", "**", "users\\kiran"]:
        with open(config.path_rules_path(home), "w") as fh:
            json.dump({"enabled_paths": [bad]}, fh)
        c = config.resolve("s1", "C:\\Users\\kiran\\proj", BASE_ENV, home)
        assert c.enabled is False, "rule %r enabled an unrelated folder" % (bad,)
        c = config.resolve("s1", "C:\\", BASE_ENV, home)
        assert c.enabled is False, "rule %r enabled a whole drive" % (bad,)


def test_windows_disabled_rule_still_wins(tmp_path, as_windows):
    home = _home(tmp_path)
    with open(config.path_rules_path(home), "w") as fh:
        json.dump({"enabled_paths": ["C:\\proj"],
                   "disabled_paths": ["C:\\proj\\secret"]}, fh)
    assert config.resolve("s1", "C:\\proj\\src", BASE_ENV, home).enabled is True
    assert config.resolve("s1", "C:\\proj\\secret\\x",
                          BASE_ENV, home).enabled is False


def test_posix_rules_are_unchanged_by_the_windows_support(tmp_path):
    """The Windows spellings must not have widened anything on POSIX."""
    home = _home(tmp_path)
    with open(config.path_rules_path(home), "w") as fh:
        json.dump({"enabled_paths": ["/opt/example"]}, fh)
    assert config.resolve("s1", "/opt/example/x", BASE_ENV, home).enabled is True
    assert config.resolve("s1", "/OPT/EXAMPLE/x", BASE_ENV, home).enabled is False, \
        "POSIX paths are case-sensitive and must stay that way"


# macOS: /tmp is a symlink to /private/tmp. /rius:enable-here stores the
# shell's $PWD (/tmp/proj) while Claude Code hands the hooks the resolved
# cwd (/private/tmp/proj), so a rule compared by spelling never matched:
# status said "on" and every hook quietly traced nothing. These use a real
# symlink, so they hold on any POSIX filesystem.


def _linked_dir(tmp_path):
    real = tmp_path / "real"
    (real / "sub").mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    return str(real), str(link)


def _write_rules(home, **rules):
    with open(config.path_rules_path(home), "w") as fh:
        json.dump(rules, fh)


def test_a_rule_spelled_through_a_symlink_matches_the_resolved_cwd(tmp_path):
    home = _home(tmp_path)
    real, link = _linked_dir(tmp_path)
    _write_rules(home, enabled_paths=[link])
    assert config.resolve("s1", real, BASE_ENV, home).enabled is True
    assert config.resolve("s1", real + "/sub", BASE_ENV, home).enabled is True


def test_a_resolved_rule_matches_a_cwd_spelled_through_a_symlink(tmp_path):
    home = _home(tmp_path)
    real, link = _linked_dir(tmp_path)
    _write_rules(home, enabled_paths=[real])
    assert config.resolve("s1", link, BASE_ENV, home).enabled is True
    assert config.resolve("s1", link + "/sub", BASE_ENV, home).enabled is True


def test_a_disable_spelled_through_a_symlink_still_beats_an_enable(tmp_path):
    home = _home(tmp_path)
    real, link = _linked_dir(tmp_path)
    _write_rules(home, enabled_paths=[real], disabled_paths=[link + "/sub"])
    assert config.resolve("s1", real + "/sub", BASE_ENV, home).enabled is False
    assert config.resolve("s1", real, BASE_ENV, home).enabled is True


def test_a_rule_whose_target_is_the_root_does_not_match_everything(tmp_path):
    """Resolving must not widen a rule past what _is_usable_rule allows: a
    rule naming a symlink to "/" resolves to "/", which matches every path."""
    home = _home(tmp_path)
    root_link = tmp_path / "root"
    root_link.symlink_to("/", target_is_directory=True)
    _write_rules(home, enabled_paths=[str(root_link)])
    assert config.resolve("s1", "/opt/unrelated", BASE_ENV, home).enabled is False


def test_disabled_below_sees_a_carve_out_spelled_the_other_way(tmp_path):
    home = _home(tmp_path)
    real, link = _linked_dir(tmp_path)
    _write_rules(home, disabled_paths=[real + "/sub"])
    assert config.disabled_below(link, home) == [real + "/sub"]


def test_same_path_merges_symlinked_spellings_only(tmp_path):
    real, link = _linked_dir(tmp_path)
    assert config.same_path(real, link)
    assert not config.same_path(real, real + "/sub")
