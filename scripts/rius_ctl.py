#!/usr/bin/env python3
"""CLI backing the /rius:* slash commands (one command file per action).

Actions: on | off | clear | enable-here | disable-here | status | login |
         login-wait | logout
Flags:   --session <id>   --cwd <path>   --env <name> (login only)

`on`, `off` and `clear` write a per-session override and therefore REFUSE
to run without an explicit `--session`: guessing the session (from the most
recently modified state file) either crashes on a fresh install or lands the
override on somebody else's concurrent session. `status` only reads, so it
may infer a session -- and says so when it does.

Default is OFF. `status` is the only thing that distinguishes a
correctly-installed-but-not-yet-enabled plugin from a broken one, so it
must surface the resolved state, the deciding layer (cfg.reason,
verbatim), the redacted key, the endpoint and, when known, spans
exported so far. Every action exits 0, including an unknown one, which
prints usage.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rius_cc import config, login, platform_compat, state  # noqa: E402

USAGE = (
    "Usage: rius_ctl.py "
    "<on|off|clear|enable-here|disable-here|status|login|login-wait|logout> "
    "[--session <id>] [--cwd <path>] [--env <production|staging>]"
)

# Actions that WRITE a per-session override. These must never guess which
# session they are acting on: guessing means either crashing on a fresh
# install (no state file -> session id "" -> the override path is the
# sessions DIRECTORY) or silently flipping tracing on for whichever other
# session happened to write state most recently.
SESSION_WRITE_ACTIONS = ("on", "off", "clear")

NO_SESSION_MESSAGE = """\
Rius: I cannot tell which session this is, so I will not guess.

`%s` writes a per-session override, and picking the wrong session would
either do nothing or turn tracing on for a different session you have open.

What to do instead:
  * `/rius:enable-here` -- enable this folder (and everything under it)
    persistently, via the path rules in ~/.claude/rius/config.json. This is
    the normal way to turn tracing on and needs no session id.
"""
SESSION_COMMAND_HINT = """\
  * `/rius:%s` -- run from inside the session; it passes the session id
    itself.
"""
SESSION_SLASH_COMMANDS = ("on", "off")


def _no_session_message(action):
    hint = SESSION_COMMAND_HINT % action if action in SESSION_SLASH_COMMANDS else ""
    return NO_SESSION_MESSAGE % action + hint


def _parse_args(argv):
    # No action (just the flags) means `status`.
    has_action = bool(argv) and not argv[0].startswith("--")
    action = argv[0] if has_action else "status"
    flags = {"--session": None, "--cwd": None, "--env": None}
    i = 1 if has_action else 0
    while i < len(argv):
        if argv[i] in flags and i + 1 < len(argv):
            flags[argv[i]] = argv[i + 1]
            i += 2
        else:
            i += 1
    return action, flags["--session"], flags["--cwd"], flags["--env"]


def _most_recent_session(home):
    """Resolve a session id from the most recently modified state file."""
    d = os.path.join(home, ".claude", "rius", "state")
    try:
        entries = [f for f in os.listdir(d) if f.endswith(".json")]
    except OSError:
        return None
    if not entries:
        return None
    entries.sort(key=lambda f: os.path.getmtime(os.path.join(d, f)),
                 reverse=True)
    return entries[0][:-len(".json")]


STILL_OFF = ("Still OFF: `%s` is disabled. Run `/rius:enable-here` in that "
             "folder instead.")


def _move_path(home, cwd, to_key, from_key):
    rules = config.read_path_rules(home)
    rules[from_key] = [p for p in config.rule_list(rules, from_key) if p != cwd]
    target = config.rule_list(rules, to_key)
    if cwd not in target:
        target.append(cwd)
    rules[to_key] = target
    config.write_path_rules(home, rules)


def _enable_here(cwd, home):
    _move_path(home, cwd, "enabled_paths", "disabled_paths")
    match = config.matching_rule(cwd, home)
    if match and not match[1]:
        print(STILL_OFF % match[0])
    else:
        print("Rius tracing enabled for %s." % cwd)


def _disable_here(cwd, home):
    _move_path(home, cwd, "disabled_paths", "enabled_paths")
    print("Rius tracing disabled for %s." % cwd)


def _spans_exported(session_id, home):
    if not session_id:
        return None
    st = state.load(session_id, home)
    return st.get("spans_exported")


def _print_status(session_id, cwd, home, inferred=False):
    cfg = config.resolve(session_id or "", cwd or "", os.environ, home)
    print("Rius tracing: %s" % ("on" if cfg.enabled else "off"))
    print("Reason: %s" % cfg.reason)
    print("cwd: %s" % (cwd or "<unknown, default>"))
    if not session_id:
        print("session: <unknown> (no --session given and no session state "
              "on disk yet)")
    elif inferred:
        print("session: %s (inferred: the most recently active session on "
              "this machine, not necessarily this one)" % session_id)
    else:
        print("session: %s" % session_id)
    print("Platform: %s" % platform_compat.describe())
    print("Endpoint: %s" % cfg.endpoint)
    print("MCP: %s" % config.mcp_url(os.environ))
    print("API key: %s" % config.redact(cfg.api_key))
    if cfg.key_source:
        print("Key from: %s" % cfg.key_source)
    _print_rule(cwd, home)
    if cfg.key_source == config.STORED_KEY_SOURCE:
        _print_account(home, login.read_credentials(home) or {})
    spans = _spans_exported(session_id, home)
    if spans is not None:
        print("Spans exported this session: %s" % spans)
    else:
        print("Spans exported this session: unknown")
    # "Spans exported: 0" with no explanation is the single most confusing
    # thing this command can print, so say why when we know.
    last = state.load(session_id, home).get("last_export_error") if session_id else None
    if last:
        print("Last export error: %s (at %s)"
              % (last.get("reason"), last.get("at")))


def _print_rule(cwd, home):
    match = config.matching_rule(cwd, home) if cwd else None
    if match is None:
        print("Rule: none matches this folder")
    else:
        print("Rule: `%s` %s this folder"
              % (match[0], "enables" if match[1] else "disables"))


def _print_account(home, creds):
    if creds.get("email"):
        print("Signed in as: %s" % creds["email"])
    if creds.get("workspace_name"):
        print("Workspace: %s%s" % (creds["workspace_name"], _in_org(creds)))
    print("Key expires: %s" % _date(creds.get("expires_at")))
    print(_mcp_hint(home, " with this key"))


def _mcp_hint(home, suffix=""):
    wanted = config.misdirected_mcp_url(os.environ, home)
    if wanted is None:
        return 'Reconnect "rius" in /mcp to query your traces%s.' % suffix
    return ('This key\'s MCP server is %s, but the bundled "rius" server points '
            "at %s. To query your traces, restart Claude Code with "
            "RIUS_MCP_URL=%s set." % (wanted, config.mcp_url(os.environ), wanted))


def _login(home, cwd, env_flag=None):
    print(login.DISCLOSURE)
    print()
    env_name = login.choose_environment(env_flag, os.environ)
    pending = login.start(home, env_name)
    if env_name != login.DEFAULT_ENVIRONMENT:
        print("Environment: %s (%s)"
              % (env_name, login.ENVIRONMENTS[env_name]["console_url"]))
    _open_browser(pending["connect_url"])
    print(_pending_line(cwd))
    print("Open:  %s" % pending["connect_url"])
    print("Code:  %s" % pending["user_code"])
    print("Check that the browser shows the same code, then pick a workspace.")


_CTL_SH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rius_ctl.sh")


def _pending_line(cwd):
    return "RIUS_LOGIN_PENDING: bash %s login-wait --cwd %s" % (
        _shell_quote(_CTL_SH), _shell_quote(cwd))


def _shell_quote(path):
    # Unquoted when safe, so the command matches the `allowed-tools` pattern
    # in commands/login.md and runs without a permission prompt.
    if all(c.isalnum() or c in "/._-~" for c in path):
        return path
    return "'" + path.replace("'", "'\\''") + "'"


def _open_browser(url):
    try:
        import webbrowser
        webbrowser.open(url)
    except Exception:  # headless or no browser: the printed URL still works
        pass


def _login_wait(home, cwd):
    previous = login.read_credentials(home)
    creds = login.wait(home)
    if creds is None:
        print("Still waiting for approval in the browser.")
        print(_pending_line(cwd))
        return
    print("Connected as %s → %s%s."
          % (creds["email"], creds["workspace_name"], _in_org(creds)))
    print("Trace this folder (%s)? Run /rius:enable-here." % cwd)
    print(_mcp_hint(home))
    moved = _moved_folders_warning(home, previous, creds)
    if moved:
        print(moved)
    if os.environ.get("RIUS_API_KEY"):
        print("NOTE: RIUS_API_KEY is set in your environment and still wins "
              "over this key. Unset it to use the new one.")


def _in_org(creds):
    return " (%s)" % creds["org_name"] if creds.get("org_name") else ""


def _moved_folders_warning(home, previous, creds):
    if not previous or previous.get("workspace_id") == creds["workspace_id"]:
        return None
    count = len(config.rule_list(config.read_path_rules(home), "enabled_paths"))
    if not count:
        return None
    return "%d enabled folder%s will now send to %s instead of %s." % (
        count, "" if count == 1 else "s", creds["workspace_name"],
        previous.get("workspace_name") or previous.get("workspace_id"))


def _logout(home, cwd):
    creds = login.read_credentials(home)
    if creds is None:
        print("No stored Rius key to remove.")
        return
    revoked = login.revoke(creds)
    login.clear_credentials(home)
    if revoked:
        print("Signed out; the key was revoked.")
    else:
        print("Signed out; could not revoke the key (it expires %s)."
              % _date(creds.get("expires_at")))


def _date(timestamp):
    return timestamp[:10] if isinstance(timestamp, str) else "unknown"


def _run_account_action(action, home, cwd, env_flag):
    cwd = cwd or os.getcwd()
    handlers = {"login": lambda: _login(home, cwd, env_flag),
                "login-wait": lambda: _login_wait(home, cwd),
                "logout": lambda: _logout(home, cwd)}
    try:
        handlers[action]()
    except (login.WaitInProgress, login.Superseded) as exc:
        print(exc)
    except login.LoginError as exc:
        print("Rius login failed: %s" % exc)


def dispatch(argv, home):
    action, session_id, cwd, env_flag = _parse_args(argv)

    if action in ("login", "login-wait", "logout"):
        _run_account_action(action, home, cwd, env_flag)
        return

    inferred = False
    if action in SESSION_WRITE_ACTIONS and not session_id:
        # Refuse loudly rather than guess. Still exit 0, like every path here.
        print(_no_session_message(action))
        return
    if not session_id:
        # `status` only READS, so inferring is safe there -- as long as it
        # says out loud that it inferred.
        session_id = _most_recent_session(home) or ""
        inferred = bool(session_id)

    if action == "on":
        config.set_session_override(session_id, home, True)
        print("Rius tracing turned on for session %s." % session_id)
    elif action == "off":
        config.set_session_override(session_id, home, False)
        print("Rius tracing turned off for session %s." % session_id)
    elif action == "clear":
        config.set_session_override(session_id, home, None)
        print("Session override cleared for session %s." % session_id)
    elif action == "enable-here":
        _enable_here(cwd or os.getcwd(), home)
    elif action == "disable-here":
        _disable_here(cwd or os.getcwd(), home)
    elif action == "status":
        _print_status(session_id, cwd, home, inferred=inferred)
    else:
        print(USAGE)


def main():
    try:
        dispatch(sys.argv[1:], platform_compat.home_dir(os.environ))
    except BaseException as exc:  # never fail this CLI
        print("rius_ctl.py error: %s" % exc)
    sys.exit(0)


if __name__ == "__main__":
    main()
