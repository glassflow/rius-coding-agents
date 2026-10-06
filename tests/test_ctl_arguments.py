"""RIUS-1222: rius_ctl.py takes only whitelisted flags and values, and what a
user types after a slash command never reaches a shell unquoted."""
import os
import pathlib
import re
import subprocess
import sys

import pytest

from tests.platforms import BASH

ROOT = pathlib.Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import rius_ctl  # noqa: E402
from rius_cc import config  # noqa: E402

KEY = {"RIUS_API_KEY": "glassflow_k"}


def _parse(argv):
    return rius_ctl._parse_args(argv)


@pytest.mark.parametrize("argv, expected", [
    (["status", "--session", "s-1_a", "--cwd", "/x"],
     ("status", {"--session": "s-1_a", "--cwd": "/x"})),
    (["on", "--session", "", "--cwd", "/x"],
     ("on", {"--session": "", "--cwd": "/x"})),
    (["enable-here", "--cwd", "/opt/my proj"],
     ("enable-here", {"--cwd": "/opt/my proj"})),
    (["login", "--env", "staging", "--cwd", "/p"],
     ("login", {"--env": "staging", "--cwd": "/p"})),
    (["login", "--cwd", "/p", "--", ""], ("login", {"--cwd": "/p"})),
    (["login", "--cwd", "/p", "--", "  --env   staging "],
     ("login", {"--cwd": "/p", "--env": "staging"})),
    (["--cwd", "/x/y"], ("status", {"--cwd": "/x/y"})),
    (["wat", "--anything"], ("wat", {})),
])
def test_whitelisted_arguments_parse(argv, expected):
    assert _parse(argv) == expected


@pytest.mark.parametrize("argv, message", [
    (["status", "--verbose"], "`status` does not accept `--verbose`"),
    (["status", "stray"], "`status` does not accept `stray`"),
    (["enable-here", "--session", "s1", "--cwd", "/x"],
     "`enable-here` does not accept `--session`; it takes only `--cwd`"),
    (["login", "--env"], "`--env` needs a value: production or staging"),
    (["login", "--env", "prod"], "`--env prod` is not accepted"),
    (["login", "--env=staging"], "`login` does not accept `--env=staging`"),
    (["on", "--session", "../../x"], "`--session ../../x` is not accepted"),
    (["on", "--session", "s1;id"], "`--session s1;id` is not accepted"),
    (["status", "--cwd", "--env"], "`--cwd --env` is not accepted"),
    (["status", "--cwd", "/a", "--cwd", "/b"], "`--cwd` was given twice"),
    (["login", "--env", "staging", "--", "--env staging"],
     "`--env` was given twice"),
])
def test_anything_else_is_refused_with_a_reason(argv, message):
    with pytest.raises(rius_ctl.ArgumentError, match=re.escape(message)):
        _parse(argv)


@pytest.mark.parametrize("action, typed", [
    ("login", "--cwd /etc"),
    ("login", "--session s2"),
    ("enable-here", "--cwd /"),
    ("status", "--session other"),
])
def test_typed_text_cannot_reach_the_flags_the_command_file_passes(action, typed):
    with pytest.raises(rius_ctl.ArgumentError, match="does not accept"):
        _parse([action, "--cwd", "/p", "--", typed])


def test_a_refused_enable_here_writes_no_rule(tmp_path, capsys, monkeypatch):
    for name, value in KEY.items():
        monkeypatch.setenv(name, value)
    rius_ctl.dispatch(["enable-here", "--cwd", "/opt/proj", "--", "x"],
                      str(tmp_path))
    out = capsys.readouterr().out
    assert "does not accept `x`" in out and "Nothing was changed." in out
    assert not os.path.exists(config.path_rules_path(str(tmp_path)))


def test_a_refused_on_writes_no_override(tmp_path, capsys):
    home = str(tmp_path)
    rius_ctl.dispatch(["on", "--session", "s1", "--force"], home)
    assert "Nothing was changed." in capsys.readouterr().out
    assert not os.path.exists(os.path.join(home, ".claude", "rius"))


def _login_line():
    text = (ROOT / "commands" / "login.md").read_text()
    return re.search(r"^!`(.+)`\s*$", text, re.M).group(1)


INJECTIONS = (
    "$(touch PWNED)",
    "`touch PWNED`",
    "; touch PWNED",
    "&& touch PWNED",
    "| touch PWNED",
    "> PWNED",
    "--env staging $(touch PWNED)",
    '" ; touch PWNED ; "',
)


@pytest.mark.parametrize("typed", INJECTIONS)
def test_typed_shell_syntax_is_refused_and_never_runs(tmp_path, typed):
    """Claude Code pastes the typed text in place of $ARGUMENTS, unquoted;
    the command file's single quotes are what keep a shell from acting on
    it, and the whitelist then refuses it."""
    home = tmp_path / "home"
    home.mkdir()
    line = _login_line().replace("$ARGUMENTS", typed)
    env = {"HOME": str(home), "PATH": os.environ["PATH"],
           "CLAUDE_PLUGIN_ROOT": str(ROOT)}
    r = subprocess.run([BASH, "-c", line], cwd=str(tmp_path),
                       capture_output=True, text=True, env=env, timeout=30)
    assert r.returncode == 0, r.stderr
    assert not (tmp_path / "PWNED").exists(), r.stdout
    assert "Nothing was changed." in r.stdout, r.stdout
    assert not os.path.exists(os.path.join(str(home), ".claude", "rius",
                                           "login_pending.json"))


def _usage_groups():
    """{action: its flags} as the usage text lists them."""
    groups, current = [], None
    for line in rius_ctl.USAGE.split("\n\n", 1)[1].splitlines():
        if re.match(r"  \S", line):
            current = [line]
            groups.append(current)
        elif current is not None and line.strip():
            current.append(line)
    listed = {}
    for group in groups:
        words = " ".join(group).split()
        stop = next(i for i, w in enumerate(words) if w[0] in "-[(")
        flags = set(re.findall(r"--[a-z]+", " ".join(words[stop:])))
        for action in (w for w in words[:stop] if w != "|"):
            listed[action] = flags - {"--agent"}
    return listed


def test_the_usage_lists_every_action_with_the_flags_it_takes():
    takes = {action: set(flags) for action, flags in rius_ctl.ACTION_FLAGS.items()}
    assert _usage_groups() == takes


def test_the_usage_says_every_action_takes_agent():
    assert "--agent <claude-code|codex|cursor>" in rius_ctl.USAGE


def test_an_unknown_action_prints_the_usage(capsys):
    rius_ctl.dispatch(["wat"], "/nonexistent-home")
    assert capsys.readouterr().out.strip() == rius_ctl.USAGE


def test_a_path_without_a_value_says_what_it_wants(capsys):
    rius_ctl.dispatch(["install-hooks", "--agent", "cursor", "--path"], "/h")
    assert ("`--path` needs a value: the path of a hooks.json file."
            in capsys.readouterr().out)
