"""RIUS-1221: each enabled folder says whether it sends content, and the
default is structure only."""
import json
import pathlib
import re
import subprocess
import sys

import pytest

from rius_cc import config
from tests.signed_in import sign_in

ROOT = pathlib.Path(__file__).parent.parent
CTL = str(ROOT / "scripts" / "rius_ctl.py")


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    (h / ".claude" / "rius").mkdir(parents=True)
    sign_in(h)
    return str(h)


def _ctl(args, home, env=None):
    e = {"HOME": home, "PATH": "/usr/bin:/bin"}
    e.update(env or {})
    return subprocess.run([sys.executable, CTL] + args, capture_output=True,
                          text=True, env=e, timeout=30)


def _rules(home):
    return config.read_path_rules(home)


def _capture(home, cwd="/opt/proj/x", env=None, session="s1"):
    return config.resolve(session, cwd, env or {}, home).capture_content


# --- the choice --------------------------------------------------------------

def test_enable_here_alone_chooses_structure_only(home):
    _ctl(["enable-here", "--cwd", "/opt/proj"], home)
    assert _rules(home)[config.CONTENT_CHOICES_KEY] == {"/opt/proj": False}
    assert _capture(home) is False


def test_with_content_is_stored_for_that_folder(home):
    _ctl(["content-on-here", "--cwd", "/opt/proj"], home)
    assert _rules(home)[config.CONTENT_CHOICES_KEY] == {"/opt/proj": True}
    assert _capture(home) is True


def test_enabling_again_without_the_flag_goes_back_to_structure(home):
    _ctl(["content-on-here", "--cwd", "/opt/proj"], home)
    _ctl(["enable-here", "--cwd", "/opt/proj"], home)
    assert _capture(home) is False


def test_each_folder_keeps_its_own_choice(home):
    _ctl(["content-on-here", "--cwd", "/opt/a"], home)
    _ctl(["enable-here", "--cwd", "/opt/b"], home)
    assert _capture(home, "/opt/a/x") is True
    assert _capture(home, "/opt/b/x") is False


def test_disabling_a_folder_drops_its_choice(home):
    _ctl(["content-on-here", "--cwd", "/opt/proj"], home)
    _ctl(["disable-here", "--cwd", "/opt/proj"], home)
    assert config.CONTENT_CHOICES_KEY not in _rules(home)


def test_a_session_on_without_an_enable_rule_sends_no_content(home):
    config.set_session_override("s1", home, True)
    c = config.resolve("s1", "/elsewhere", {}, home)
    assert (c.enabled, c.capture_content) == (True, False)


# --- rules from before the choice existed -------------------------------------

def _legacy_rule(home):
    with open(config.path_rules_path(home), "w") as fh:
        json.dump({"enabled_paths": ["/opt/proj"], "disabled_paths": []}, fh)


def test_a_rule_without_a_choice_keeps_sending_content(home):
    _legacy_rule(home)
    c = config.resolve("s1", "/opt/proj/x", {}, home)
    assert c.capture_content is True
    assert c.unchosen_rule == "/opt/proj"


def test_status_suggests_picking_for_a_rule_without_a_choice(home):
    _legacy_rule(home)
    out = _ctl(["status", "--session", "s1", "--cwd", "/opt/proj"], home).stdout
    assert ("Rule `/opt/proj` predates the content choice, so it still sends "
            "content. Pick one: /rius:enable-here (structure only, "
            "recommended) or /rius:enable-content-here.") in out.splitlines()
    assert "Content: prompts, replies, file contents and command output" in out


def test_status_names_the_mode_and_no_notice_once_chosen(home):
    _ctl(["enable-here", "--cwd", "/opt/proj"], home)
    out = _ctl(["status", "--session", "s1", "--cwd", "/opt/proj"], home).stdout
    assert "Content: none (structure only)" in out.splitlines()
    assert "predates the content choice" not in out


# --- the environment only lowers it -------------------------------------------

def test_the_env_cannot_raise_capture_above_the_folder_s_choice(home):
    _ctl(["enable-here", "--cwd", "/opt/proj"], home)
    c = config.resolve("s1", "/opt/proj", {"RIUS_CAPTURE_CONTENT": "true"}, home)
    assert c.capture_content is False
    assert config.IGNORED_CAPTURE in c.ignored_env


def test_the_env_can_lower_a_folder_that_chose_content(home):
    _ctl(["content-on-here", "--cwd", "/opt/proj"], home)
    c = config.resolve("s1", "/opt/proj", {"RIUS_CAPTURE_CONTENT": "false"}, home)
    assert c.capture_content is False
    assert c.ignored_env == []


def test_the_env_lowering_a_legacy_rule_needs_no_notice(home):
    _legacy_rule(home)
    c = config.resolve("s1", "/opt/proj", {"RIUS_CAPTURE_CONTENT": "false"}, home)
    assert (c.capture_content, c.unchosen_rule) == (False, None)


# --- only the user can choose content -----------------------------------------

def _grants(command_file):
    head = command_file.read_text().split("---")[1]
    return re.findall(r"Bash\(([^)]*)\)", head)


@pytest.mark.parametrize("name", ["enable-here", "enable-content-here"])
def test_the_model_cannot_invoke_either_enable_command(name):
    head = (ROOT / "commands" / (name + ".md")).read_text().split("---")[1]
    assert "disable-model-invocation: true" in head.splitlines()


CONTENT_CALL = 'bash "${CLAUDE_PLUGIN_ROOT}/scripts/rius_ctl.sh" content-on-here --cwd /p'


@pytest.mark.parametrize("command_file", sorted(
    p for p in (ROOT / "commands").glob("*.md") if p.stem != "enable-content-here"))
def test_no_other_command_s_grant_reaches_the_content_subcommand(command_file):
    """A grant is a prefix (`...:*`). During any other command's turn, a
    prompt-injected model must not be pre-approved to turn content on."""
    for grant in _grants(command_file):
        if grant.endswith(":*"):
            assert not CONTENT_CALL.startswith(grant[:-2]), (command_file.name, grant)
        else:
            assert "content-on-here" not in grant, (command_file.name, grant)


def test_the_content_command_is_granted_only_its_own_subcommand():
    assert _grants(ROOT / "commands" / "enable-content-here.md") == [
        'bash "${CLAUDE_PLUGIN_ROOT}/scripts/rius_ctl.sh" content-on-here:*']


@pytest.mark.parametrize("flag", ["--with-content", "--content", "-c"])
def test_enable_here_refuses_any_content_flag(home, flag):
    r = _ctl(["enable-here", flag, "--cwd", "/opt/proj"], home)
    assert "/rius:enable-content-here" in r.stdout
    assert config.read_path_rules(home) == {}
