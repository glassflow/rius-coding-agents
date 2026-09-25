#!/usr/bin/env python3
"""CLI backing the /rius slash command.

Actions: on | off | clear | enable-here | status | login | login-wait | logout
Flags:   --session <id>   --cwd <path>

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
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rius_cc import config, login, platform_compat, state  # noqa: E402

USAGE = (
    "Usage: rius_ctl.py "
    "<on|off|clear|enable-here|status|login|login-wait|logout> "
    "[--session <id>] [--cwd <path>]"
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
  * `/rius enable-here` -- enable this folder (and everything under it)
    persistently, via the path rules in ~/.claude/rius/config.json. This is
    the normal way to turn tracing on and needs no session id.
  * `/rius status` -- prints the session id it can see; then run
    `/rius %s --session <that id>` if you really want a one-session override.
"""


def _parse_args(argv):
    action = argv[0] if argv else ""
    session_id = None
    cwd = None
    i = 1
    while i < len(argv):
        if argv[i] == "--session" and i + 1 < len(argv):
            session_id = argv[i + 1]
            i += 2
        elif argv[i] == "--cwd" and i + 1 < len(argv):
            cwd = argv[i + 1]
            i += 2
        else:
            i += 1
    return action, session_id, cwd


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


def _read_path_rules(home):
    path = config.path_rules_path(home)
    try:
        with open(path) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def _write_path_rules(home, rules):
    path = config.path_rules_path(home)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        json.dump(rules, fh)


def _enable_here(cwd, home):
    rules = _read_path_rules(home)
    enabled_paths = rules.get("enabled_paths")
    if not isinstance(enabled_paths, list):
        enabled_paths = []
    if cwd not in enabled_paths:
        enabled_paths.append(cwd)
    rules["enabled_paths"] = enabled_paths
    _write_path_rules(home, rules)


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
    print("API key: %s" % config.redact(cfg.api_key))
    if cfg.key_source:
        print("Key from: %s" % cfg.key_source)
    if cfg.workspace_name:
        print("Workspace: %s" % cfg.workspace_name)
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


def _login(home):
    print(login.DISCLOSURE)
    print()
    pending = login.start(home)
    _open_browser(pending["verification_uri_complete"])
    print("RIUS_LOGIN_PENDING: bash %s login-wait" % _shell_quote(_CTL_SH))
    print("Open:  %s" % pending["verification_uri_complete"])
    print("Code:  %s" % pending["user_code"])
    print("Confirm the code matches the one in your browser, then approve.")


_CTL_SH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rius_ctl.sh")


def _shell_quote(path):
    # Unquoted when safe, so the command matches the `allowed-tools` pattern
    # in commands/rius.md and runs without a permission prompt.
    if all(c.isalnum() or c in "/._-~" for c in path):
        return path
    return "'" + path.replace("'", "'\\''") + "'"


def _open_browser(url):
    try:
        import webbrowser
        webbrowser.open(url)
    except Exception:  # headless or no browser: the printed URL still works
        pass


def _login_wait(home):
    creds = login.wait(home)
    if creds is None:
        print("Still waiting for approval in the browser. "
              "Run `/rius login-wait` again once you have approved.")
        return
    print("Signed in. Traces will go to workspace: %s"
          % (creds.get("workspace_name") or creds.get("workspace_id")))
    print("Key %s (scopes: %s) saved to %s"
          % (config.redact(creds["api_key"]), ", ".join(creds["scopes"]),
             login.credentials_path(home)))
    if creds.get("expires_at"):
        print("It expires %s." % creds["expires_at"])
    if os.environ.get("RIUS_API_KEY"):
        print("NOTE: RIUS_API_KEY is set in your environment and still wins "
              "over this key. Unset it to use the new one.")
    print("The plugin's `rius` MCP server uses this key too: run `/mcp` and "
          "reconnect `rius` (or restart Claude Code) to query your traces.")
    print("Nothing is traced yet. Run `/rius enable-here` in a folder to "
          "start tracing it (the first spans can take ~30s to be accepted).")


def _logout(home):
    if login.clear_credentials(home):
        print("Removed the stored Rius key. It is still valid on the server "
              "until it expires or is revoked in the console.")
    else:
        print("No stored Rius key to remove.")


def _run_account_action(action, home):
    try:
        {"login": _login, "login-wait": _login_wait, "logout": _logout}[action](home)
    except login.LoginError as exc:
        print("Rius login failed: %s" % exc)


def dispatch(argv, home):
    action, session_id, cwd = _parse_args(argv)

    if action in ("login", "login-wait", "logout"):
        _run_account_action(action, home)
        return

    inferred = False
    if action in SESSION_WRITE_ACTIONS and not session_id:
        # Refuse loudly rather than guess. Still exit 0, like every path here.
        print(NO_SESSION_MESSAGE % (action, action))
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
        target_cwd = cwd or os.getcwd()
        _enable_here(target_cwd, home)
        print("Rius tracing enabled for %s." % target_cwd)
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
