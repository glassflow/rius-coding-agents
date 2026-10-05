import json
import subprocess
import sys
import pathlib

import pytest

from tests.platforms import BASH, IS_WINDOWS, minimal_env, posix_only

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


def test_the_no_session_hint_names_only_commands_that_exist(tmp_path):
    home = _fresh_home(tmp_path)
    commands = pathlib.Path(__file__).parent.parent / "commands"
    for action in ("on", "off", "clear"):
        r = _run([action], home, {"RIUS_API_KEY": "glassflow_k"})
        hinted = (commands / (action + ".md")).exists()
        assert ("/rius:%s" % action in r.stdout) == hinted, action


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
COMMANDS = ROOT / "commands"
CTL_SH = str(ROOT / "scripts" / "rius_ctl.sh")

PLAIN_ACTIONS = ("login", "enable-here", "disable-here", "logout")
SESSION_ACTIONS = ("status", "on", "off")
ALL_ACTIONS = PLAIN_ACTIONS + SESSION_ACTIONS


def _command_file(action):
    return COMMANDS / ("%s.md" % action)


def _frontmatter(action):
    return _command_file(action).read_text().split("---")[1]


def _command_line(action):
    """The one `!`...`` bash substitution Claude Code runs for /rius:<action>."""
    m = re.search(r"^!`(.+)`\s*$", _command_file(action).read_text(), re.M)
    assert m, "commands/%s.md has no bash substitution line" % action
    return m.group(1)


def test_the_catch_all_command_is_gone():
    assert not (COMMANDS / "rius.md").exists()
    assert sorted(p.stem for p in COMMANDS.glob("*.md")) == sorted(ALL_ACTIONS)


@pytest.mark.parametrize("action", ALL_ACTIONS)
def test_command_frontmatter_permits_the_launcher(action):
    front = _frontmatter(action)
    assert re.search(r"^description: \S", front, re.M), front
    assert ("allowed-tools: Bash(bash ${CLAUDE_PLUGIN_ROOT}/scripts/rius_ctl.sh:*)"
            in front), front


@pytest.mark.parametrize("action", PLAIN_ACTIONS)
def test_command_passes_its_action_and_the_arguments(action):
    assert _command_line(action) == (
        'bash "${CLAUDE_PLUGIN_ROOT}/scripts/rius_ctl.sh" %s $ARGUMENTS '
        '--cwd "$PWD"' % action)


@pytest.mark.parametrize("action", SESSION_ACTIONS)
def test_session_commands_pass_the_session_id(action):
    assert _command_line(action) == (
        'bash "${CLAUDE_PLUGIN_ROOT}/scripts/rius_ctl.sh" %s '
        '--session ${CLAUDE_SESSION_ID} $ARGUMENTS --cwd "$PWD"' % action)


@pytest.mark.parametrize("action", ALL_ACTIONS)
def test_slash_command_does_not_rely_on_the_shebang(action):
    """Windows has no shebang support, so a command file that executes
    rius_ctl.py directly does nothing there."""
    line = _command_line(action)
    assert "rius_ctl.sh" in line, line
    assert not re.search(r"rius_ctl\.py", line), line


@pytest.mark.parametrize("action", ALL_ACTIONS)
def test_slash_command_target_exists_on_disk(action):
    named = re.findall(r"\$\{CLAUDE_PLUGIN_ROOT\}(/[\w./-]+)",
                       _command_line(action))
    assert named
    for rel in named:
        assert (ROOT / rel.lstrip("/")).exists(), rel


def test_login_command_tells_claude_to_wait_in_the_background():
    body = _command_file("login").read_text().split("---", 2)[2]
    for needle in ("RIUS_LOGIN_PENDING:", "Open:", "Code:", "run_in_background",
                   "Still waiting", "/rius:enable-here"):
        assert needle in body, needle


def test_slash_command_line_runs_end_to_end(tmp_path):
    """Run the literal line from the command file, not an approximation."""
    home = tmp_path / "home"
    (home / ".claude" / "rius").mkdir(parents=True)
    env = {"HOME": str(home), "PATH": os.environ["PATH"],
           "CLAUDE_PLUGIN_ROOT": str(ROOT), "RIUS_API_KEY": "glassflow_k",
           "CLAUDE_SESSION_ID": "s-e2e"}
    line = _command_line("status").replace("$ARGUMENTS", "")
    r = subprocess.run([BASH, "-c", line], cwd=str(tmp_path),
                       capture_output=True, text=True, env=env, timeout=30)
    assert r.returncode == 0, r.stderr
    assert "off" in r.stdout.lower()
    assert "session: s-e2e\n" in r.stdout
    assert "glassflow_k" not in r.stdout



def test_enable_here_command_line_enables_the_folder_the_hooks_see(tmp_path):
    """The rule comes from the shell's $PWD; the hook's cwd from Claude
    Code's process.cwd(). On Windows the first is a Git Bash path (/c/...)
    and the second a native one (C:\\...), so this is the one place the
    two spellings meet."""
    home = tmp_path / "home"
    (home / ".claude" / "rius").mkdir(parents=True)
    proj = tmp_path / "proj"
    proj.mkdir()
    env = {"HOME": str(home), "PATH": os.environ["PATH"],
           "CLAUDE_PLUGIN_ROOT": str(ROOT), "RIUS_API_KEY": "glassflow_k"}
    line = _command_line("enable-here").replace("$ARGUMENTS", "")
    r = subprocess.run([BASH, "-c", line], cwd=str(proj),
                       capture_output=True, text=True, env=env, timeout=30)
    assert r.returncode == 0, r.stderr
    native_cwd = subprocess.run(
        [sys.executable, "-c", "import os; print(os.getcwd())"],
        cwd=str(proj), capture_output=True, text=True, check=True).stdout.strip()
    from rius_cc import config
    assert config.resolve("s1", native_cwd, KEY, str(home)).enabled, (
        "enable-here wrote %r, which does not cover the hook's cwd %r"
        % (config.read_path_rules(str(home)), native_cwd))

PRINTED_TREES = ("scripts", "docs/getting-started.md", "docs/install.md",
                 "docs/how-it-works.md", "docs/api-keys.md", "README.md", "CHANGELOG.md")


def _text_files():
    for tree in PRINTED_TREES:
        path = ROOT / tree
        files = [path] if path.is_file() else sorted(path.rglob("*"))
        for f in files:
            if f.is_file() and f.suffix in (".py", ".sh", ".md"):
                yield f


def test_no_string_names_the_old_space_separated_command():
    stale = re.compile(r"/rius [a-z]")
    hits = ["%s:%d: %s" % (f.relative_to(ROOT), n, line.strip())
            for f in _text_files()
            for n, line in enumerate(f.read_text().splitlines(), 1)
            if stale.search(line)]
    assert not hits, "\n".join(hits)


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
    env = minimal_env(HOME=str(home), PATH=str(fake))
    r = subprocess.run([BASH, CTL_SH, "status", "--cwd", "/x"],
                       capture_output=True, text=True, env=env, timeout=30)
    assert r.returncode == 0
    assert "python" in r.stdout.lower()


def test_ctl_names_a_missing_find_python_sh_instead_of_blaming_path(tmp_path):
    """Minor 1. Same misdiagnosis as hook.sh: if scripts/_find_python.sh is
    absent, the guard must not silently leave rius_candidates unset -- that
    prints "tried: ()" and blames PATH for what is really a packaging fault.
    Copy rius_ctl.sh alone into a directory with no _find_python.sh."""
    import shutil
    home = tmp_path / "home"
    (home / ".claude" / "rius").mkdir(parents=True)
    bare = tmp_path / "bare_scripts"
    bare.mkdir()
    shutil.copy(CTL_SH, str(bare / "rius_ctl.sh"))
    # deliberately no rius_ctl.py, no _find_python.sh copied alongside
    env = minimal_env(HOME=str(home), PATH=os.environ["PATH"])
    r = subprocess.run([BASH, str(bare / "rius_ctl.sh"), "status",
                       "--cwd", "/x"],
                       capture_output=True, text=True, env=env, timeout=30)
    assert r.returncode == 0
    assert "_find_python.sh" in r.stdout, (
        "output did not name the missing file: %r" % r.stdout)
    assert "tried: ()" not in r.stdout, (
        "output still shows the empty-candidate-list PATH misdiagnosis")


def test_bare_invocation_means_status(tmp_path):
    # `/rius` with no argument reaches the script as just `--cwd <path>`.
    home = str(tmp_path)
    r = _run(["--cwd", "/x/y"], home)
    assert "Rius tracing: off" in r.stdout
    assert "cwd: /x/y" in r.stdout


def test_manifests_name_the_plugin_rius_at_0_5_0():
    plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
    market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
    listed = {p["name"]: p["version"] for p in market["plugins"]}
    assert (plugin["name"], plugin["version"]) == ("rius", "0.5.0")
    assert listed == {"rius": "0.5.0"}


def test_pyproject_version_matches_the_manifests():
    pyproject = (ROOT / "pyproject.toml").read_text()
    plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
    assert 'version = "%s"\n' % plugin["version"] in pyproject


# --- disable-here / enable-here -----------------------------------------------

def _rules(tmp_path):
    return json.load(open(str(tmp_path / ".claude" / "rius" / "config.json")))


def _config_is_private(tmp_path):
    """0600 on POSIX; Windows has no permission bits for this to set."""
    path = str(tmp_path / ".claude" / "rius" / "config.json")
    return IS_WINDOWS or os.stat(path).st_mode & 0o777 == 0o600


KEY = {"RIUS_API_KEY": "glassflow_k"}


def test_disable_here_beats_a_parent_enable(tmp_path):
    home = _fresh_home(tmp_path)
    _run(["enable-here", "--cwd", "/opt"], home, KEY)
    r = _run(["disable-here", "--cwd", "/opt/proj"], home, KEY)
    assert r.returncode == 0
    assert "disabled for /opt/proj" in r.stdout
    assert _rules(tmp_path) == {"enabled_paths": ["/opt"],
                                "disabled_paths": ["/opt/proj"]}
    status = _run(["status", "--session", "s1", "--cwd", "/opt/proj/sub"], home, KEY)
    assert "Rius tracing: off" in status.stdout
    status = _run(["status", "--session", "s1", "--cwd", "/opt/other"], home, KEY)
    assert "Rius tracing: on" in status.stdout


def test_disable_here_removes_the_folder_from_enabled_paths(tmp_path):
    home = _fresh_home(tmp_path)
    _run(["enable-here", "--cwd", "/opt/proj"], home, KEY)
    _run(["disable-here", "--cwd", "/opt/proj"], home, KEY)
    assert _rules(tmp_path) == {"enabled_paths": [], "disabled_paths": ["/opt/proj"]}


def test_enable_here_under_a_disabled_parent_says_still_off(tmp_path):
    home = _fresh_home(tmp_path)
    _run(["disable-here", "--cwd", "/opt"], home, KEY)
    r = _run(["enable-here", "--cwd", "/opt/proj"], home, KEY)
    assert r.returncode == 0
    assert r.stdout.strip() == ("Still OFF: `/opt` is disabled. Run "
                                "`/rius:enable-here` in that folder instead.")
    status = _run(["status", "--session", "s1", "--cwd", "/opt/proj"], home, KEY)
    assert "Rius tracing: off" in status.stdout


def test_enable_here_removes_the_disabled_entry(tmp_path):
    home = _fresh_home(tmp_path)
    _run(["disable-here", "--cwd", "/opt/proj"], home, KEY)
    r = _run(["enable-here", "--cwd", "/opt/proj"], home, KEY)
    assert r.returncode == 0
    assert "enabled for /opt/proj" in r.stdout
    assert _rules(tmp_path) == {"enabled_paths": ["/opt/proj"], "disabled_paths": []}


def _store_login(home, workspace_name="eng-shared"):
    from rius_cc import login as _login
    _login._write_private(_login.credentials_path(home), {
        "api_key": "ri_stored", "endpoint": "https://ingest", "env": "production",
        "workspace_id": "w", "workspace_name": workspace_name,
        "email": "x@acme.com", "expires_at": "2026-12-26T00:00:00Z"})


def test_enable_here_says_what_this_folder_will_upload_and_where(tmp_path):
    home = _fresh_home(tmp_path)
    _store_login(home)
    r = _run(["enable-here", "--cwd", "/opt/proj"], home)
    assert r.stdout.splitlines() == [
        "Rius tracing enabled for /opt/proj and everything under it.",
        "Sessions here now send prompts, replies, the contents of files Claude "
        "reads and command output to eng-shared.",
        "Set RIUS_CAPTURE_CONTENT=false to send structure only (models, "
        "tokens, timing), or run /rius:disable-here to stop."]


def test_enable_here_with_content_off_does_not_claim_content_is_sent(tmp_path):
    home = _fresh_home(tmp_path)
    _store_login(home)
    r = _run(["enable-here", "--cwd", "/opt/proj"], home,
             {"RIUS_CAPTURE_CONTENT": "false"})
    assert r.stdout.splitlines() == [
        "Rius tracing enabled for /opt/proj and everything under it.",
        "Sessions here now send structure only (models, tokens, timing) to "
        "eng-shared; RIUS_CAPTURE_CONTENT=false withholds prompts, replies, "
        "file contents and command output.",
        "Run /rius:disable-here to stop."]


def test_enable_here_under_an_env_key_names_no_workspace_it_cannot_know(tmp_path):
    home = _fresh_home(tmp_path)
    _store_login(home)
    r = _run(["enable-here", "--cwd", "/opt/proj"], home, KEY)
    assert "command output to your Rius workspace." in r.stdout
    assert "eng-shared" not in r.stdout


def test_enable_here_before_login_says_nothing_is_sent_yet(tmp_path):
    home = _fresh_home(tmp_path)
    r = _run(["enable-here", "--cwd", "/opt/proj"], home)
    assert r.stdout.splitlines()[1] == (
        "Sessions here will send prompts, replies, the contents of files "
        "Claude reads and command output to the workspace you pick once you "
        "sign in with /rius:login.")
    assert "now send" not in r.stdout


def test_enable_here_names_the_disabled_folders_it_does_not_cover(tmp_path):
    home = _fresh_home(tmp_path)
    _store_login(home)
    _run(["disable-here", "--cwd", "/opt/proj/secrets"], home)
    _run(["disable-here", "--cwd", "/opt/proj/vendor"], home)
    _run(["disable-here", "--cwd", "/opt/other"], home)
    r = _run(["enable-here", "--cwd", "/opt/proj"], home)
    assert r.stdout.splitlines()[0] == (
        "Rius tracing enabled for /opt/proj and everything under it, except "
        "/opt/proj/secrets and /opt/proj/vendor (disabled).")
    status = _run(["status", "--session", "s1", "--cwd", "/opt/proj/secrets/x"], home)
    assert "Rius tracing: off" in status.stdout


def test_a_sibling_with_a_shared_prefix_is_not_an_exception(tmp_path):
    home = _fresh_home(tmp_path)
    _store_login(home)
    _run(["disable-here", "--cwd", "/opt/project-b"], home)
    r = _run(["enable-here", "--cwd", "/opt/proj"], home)
    assert r.stdout.splitlines()[0] == (
        "Rius tracing enabled for /opt/proj and everything under it.")


@pytest.mark.parametrize("action", ["enable-here", "disable-here"])
def test_path_rules_are_written_privately(tmp_path, action):
    home = _fresh_home(tmp_path)
    _run([action, "--cwd", "/opt/proj"], home, KEY)
    assert _config_is_private(tmp_path)


def test_status_names_the_matching_rule(tmp_path):
    home = _fresh_home(tmp_path)
    _run(["enable-here", "--cwd", "/opt"], home, KEY)
    _run(["disable-here", "--cwd", "/opt/proj"], home, KEY)
    on = _run(["status", "--session", "s1", "--cwd", "/opt/x"], home, KEY)
    off = _run(["status", "--session", "s1", "--cwd", "/opt/proj/y"], home, KEY)
    none = _run(["status", "--session", "s1", "--cwd", "/elsewhere"], home, KEY)
    assert "Rule: `/opt` enables this folder" in on.stdout
    assert "Rule: `/opt/proj` disables this folder" in off.stdout
    assert "Rule: none matches this folder" in none.stdout


def test_status_names_the_signed_in_account_and_the_mcp_hint(tmp_path):
    home = _fresh_home(tmp_path)
    from rius_cc import login as _login
    _login._write_private(_login.credentials_path(home), {
        "api_key": "ri_secret", "endpoint": "https://ingest", "env": "production",
        "workspace_id": "w", "workspace_name": "eng-shared", "org_name": "Acme",
        "email": "x@acme.com", "expires_at": "2026-12-26T00:00:00Z"})
    r = _run(["status", "--session", "s1", "--cwd", "/x"], home)
    assert "Signed in as: x@acme.com" in r.stdout
    assert "Workspace: eng-shared (Acme)" in r.stdout
    assert "Key expires: 2026-12-26" in r.stdout
    assert 'Reconnect "rius" in /mcp' in r.stdout
    assert "ri_secret" not in r.stdout


# --- a session stopped by a mid-session disable -------------------------------

STOPPED_NOTE = ("Stopped: this session stopped tracing when its folder was "
                "disabled, and stays stopped even if the folder is enabled "
                "again. New sessions in this folder are traced as usual.")


def _stop_session(home, sid="s1"):
    from rius_cc import state as _state
    _state.save(sid, home, {"root_started": True, "content_stopped": True})


def test_status_of_a_stopped_session_in_an_enabled_folder_says_off(tmp_path):
    home = _fresh_home(tmp_path)
    _run(["enable-here", "--cwd", "/opt/proj"], home, KEY)
    _stop_session(home)
    r = _run(["status", "--session", "s1", "--cwd", "/opt/proj"], home, KEY)
    assert "Rius tracing: off" in r.stdout
    assert "Rius tracing: on" not in r.stdout
    assert STOPPED_NOTE in r.stdout


def test_status_of_a_stopped_session_in_a_disabled_folder_explains_it(tmp_path):
    home = _fresh_home(tmp_path)
    _run(["disable-here", "--cwd", "/opt/proj"], home, KEY)
    _stop_session(home)
    r = _run(["status", "--session", "s1", "--cwd", "/opt/proj"], home, KEY)
    assert "Rius tracing: off" in r.stdout
    assert STOPPED_NOTE in r.stdout


def test_status_of_a_session_that_was_never_stopped_is_unchanged(tmp_path):
    home = _fresh_home(tmp_path)
    _run(["enable-here", "--cwd", "/opt/proj"], home, KEY)
    r = _run(["status", "--session", "s1", "--cwd", "/opt/proj"], home, KEY)
    assert "Rius tracing: on" in r.stdout
    assert "Stopped:" not in r.stdout


def _linked_dir(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    return str(real), str(link)


@posix_only("resolved() leaves Windows paths as written, by design")
def test_status_agrees_with_the_hooks_across_a_symlink(tmp_path):
    """enable-here gets the shell's $PWD (/tmp/proj on macOS); the hooks get
    Claude Code's resolved cwd (/private/tmp/proj). Status run from either
    spelling must give the hooks' answer."""
    home = _fresh_home(tmp_path)
    real, link = _linked_dir(tmp_path)
    _run(["enable-here", "--cwd", link], home, KEY)
    for cwd in (link, real):
        r = _run(["status", "--session", "s1", "--cwd", cwd], home, KEY)
        assert "Rius tracing: on" in r.stdout, cwd


@posix_only("resolved() leaves Windows paths as written, by design")
def test_status_shows_the_folder_the_hooks_see(tmp_path):
    home = _fresh_home(tmp_path)
    real, link = _linked_dir(tmp_path)
    r = _run(["status", "--session", "s1", "--cwd", link], home, KEY)
    assert "cwd: %s (hooks see %s)" % (link, real) in r.stdout


@posix_only("resolved() leaves Windows paths as written, by design")
def test_disable_here_through_a_symlink_replaces_the_enable(tmp_path):
    home = _fresh_home(tmp_path)
    real, link = _linked_dir(tmp_path)
    _run(["enable-here", "--cwd", link], home, KEY)
    _run(["disable-here", "--cwd", real], home, KEY)
    assert _rules(tmp_path) == {"enabled_paths": [], "disabled_paths": [real]}


@posix_only("resolved() leaves Windows paths as written, by design")
def test_enable_here_is_idempotent_across_spellings(tmp_path):
    home = _fresh_home(tmp_path)
    real, link = _linked_dir(tmp_path)
    _run(["enable-here", "--cwd", link], home, KEY)
    _run(["enable-here", "--cwd", real], home, KEY)
    assert _rules(tmp_path) == {"enabled_paths": [real], "disabled_paths": []}


@posix_only("resolved() leaves Windows paths as written, by design")
def test_enable_here_stores_the_folder_the_hooks_see(tmp_path):
    home = _fresh_home(tmp_path)
    real, link = _linked_dir(tmp_path)
    r = _run(["enable-here", "--cwd", link], home, KEY)
    assert "enabled for %s " % real in r.stdout
    assert _rules(tmp_path) == {"enabled_paths": [real], "disabled_paths": []}


@posix_only("resolved() leaves Windows paths as written, by design")
def test_status_decides_for_the_folder_the_hooks_see(tmp_path):
    """$PWD ~/proj/vendor, where vendor -> ~/secret: the hooks are handed
    ~/secret, which no rule enables, so status must not say on."""
    home = _fresh_home(tmp_path)
    proj = tmp_path / "proj"
    proj.mkdir()
    secret = tmp_path / "secret"
    secret.mkdir()
    (proj / "vendor").symlink_to(secret, target_is_directory=True)
    _run(["enable-here", "--cwd", str(proj)], home, KEY)
    r = _run(["status", "--session", "s1", "--cwd", str(proj / "vendor")],
             home, KEY)
    assert "Rius tracing: off" in r.stdout


@posix_only("resolved() leaves Windows paths as written, by design")
def test_status_names_a_rule_written_through_a_symlink(tmp_path):
    """0.4.3 stored $PWD as written, so its /tmp rules never matched."""
    home = _fresh_home(tmp_path)
    real, link = _linked_dir(tmp_path)
    with open(str(tmp_path / ".claude" / "rius" / "config.json"), "w") as fh:
        json.dump({"enabled_paths": [link], "disabled_paths": []}, fh)
    r = _run(["status", "--session", "s1", "--cwd", link], home, KEY)
    assert "Rius tracing: off" in r.stdout
    assert ("Rule `%s` enables nothing: Claude Code calls that folder %s. "
            "Run /rius:enable-here in %s to fix it." % (link, real, link)
            ) in r.stdout


@posix_only("resolved() leaves Windows paths as written, by design")
def test_status_points_a_stale_disable_at_its_own_folder(tmp_path):
    home = _fresh_home(tmp_path)
    real, link = _linked_dir(tmp_path)
    (tmp_path / "real" / "sub").mkdir()
    with open(str(tmp_path / ".claude" / "rius" / "config.json"), "w") as fh:
        json.dump({"enabled_paths": [], "disabled_paths": [link]}, fh)
    r = _run(["status", "--session", "s1", "--cwd", link + "/sub"], home, KEY)
    assert ("Rule `%s` disables nothing: Claude Code calls that folder %s. "
            "Run /rius:disable-here in %s to fix it." % (link, real, link)
            ) in r.stdout


@posix_only("resolved() leaves Windows paths as written, by design")
def test_enabling_a_parent_again_keeps_an_old_carve_out_off(tmp_path):
    """0.4.3 rules: enable link, disable link/sub; neither matched. Re-running
    enable-here on the parent must not start tracing sub."""
    home = _fresh_home(tmp_path)
    real, link = _linked_dir(tmp_path)
    (tmp_path / "real" / "sub").mkdir()
    (tmp_path / "real" / "sab").mkdir()
    with open(str(tmp_path / ".claude" / "rius" / "config.json"), "w") as fh:
        json.dump({"enabled_paths": [link],
                   "disabled_paths": [link + "/sub", link + "/sa*"]}, fh)
    r = _run(["enable-here", "--cwd", link], home, KEY)
    assert "except %s/sub and %s/sa* (disabled)" % (real, real) in r.stdout
    for sub in ("sub", "sab"):
        status = _run(["status", "--session", "s1", "--cwd", real + "/" + sub],
                      home, KEY)
        assert "Rius tracing: off" in status.stdout, sub


@pytest.mark.parametrize("action", ["enable-here", "disable-here"])
def test_a_folder_too_broad_for_a_rule_is_refused_out_loud(tmp_path, action):
    home = _fresh_home(tmp_path)
    r = _run([action, "--cwd", "/"], home, KEY)
    assert r.stdout.startswith("Not changed: `/` is too broad for a rule")
    assert not (tmp_path / ".claude" / "rius" / "config.json").exists()


@posix_only("resolved() leaves Windows paths as written, by design")
def test_enable_here_in_a_folder_typed_in_the_wrong_case(tmp_path):
    probe = tmp_path / "caseprobe"
    probe.mkdir()
    if not (tmp_path / "CASEPROBE").is_dir():
        pytest.skip("needs a case-insensitive filesystem")
    home = _fresh_home(tmp_path)
    (tmp_path / "abc").mkdir()
    _run(["enable-here", "--cwd", str(tmp_path / "ABC")], home, KEY)
    assert _rules(tmp_path)["enabled_paths"] == [str(tmp_path / "abc")]


@posix_only("resolved() leaves Windows paths as written, by design")
def test_enable_here_replaces_a_rule_written_through_a_symlink(tmp_path):
    home = _fresh_home(tmp_path)
    real, link = _linked_dir(tmp_path)
    with open(str(tmp_path / ".claude" / "rius" / "config.json"), "w") as fh:
        json.dump({"enabled_paths": [link], "disabled_paths": []}, fh)
    _run(["enable-here", "--cwd", link], home, KEY)
    assert _rules(tmp_path) == {"enabled_paths": [real], "disabled_paths": []}
    r = _run(["status", "--session", "s1", "--cwd", link], home, KEY)
    assert "Rius tracing: on" in r.stdout
    assert "through a symlink" not in r.stdout


NO_HOOK_HINT = "No Rius hook has traced this session yet."


def test_status_says_when_no_hook_has_run_in_this_session(tmp_path):
    home = _fresh_home(tmp_path)
    _run(["enable-here", "--cwd", "/opt/proj"], home, KEY)
    r = _run(["status", "--session", "s1", "--cwd", "/opt/proj"], home, KEY)
    assert NO_HOOK_HINT in r.stdout


def test_status_drops_the_hint_once_a_hook_has_traced(tmp_path):
    from rius_cc import state as _state
    home = _fresh_home(tmp_path)
    _run(["enable-here", "--cwd", "/opt/proj"], home, KEY)
    _state.save("s1", home, _state.new_state())
    r = _run(["status", "--session", "s1", "--cwd", "/opt/proj"], home, KEY)
    assert NO_HOOK_HINT not in r.stdout


def test_status_in_a_folder_that_is_off_gives_no_hook_hint(tmp_path):
    """Off is explained by the Reason line; hooks there write nothing."""
    home = _fresh_home(tmp_path)
    r = _run(["status", "--session", "s1", "--cwd", "/opt/proj"], home, KEY)
    assert "Rius tracing: off" in r.stdout
    assert NO_HOOK_HINT not in r.stdout
