#!/usr/bin/env python3
"""CLI backing the /rius slash command.

Actions: on | off | clear | enable-here | status | login | login-wait |
         claim <number|name|id> | provision | logout
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

from rius_cc import anonymous, config, login, platform_compat, state  # noqa: E402

USAGE = (
    "Usage: rius_ctl.py "
    "<on|off|clear|enable-here|status|login|login-wait|claim <n|name|id>|"
    "provision|logout> [--session <id>] [--cwd <path>]"
)

ACCOUNT_ACTIONS = ("login", "login-wait", "logout", "provision", "claim")

CLAIM_DISCLOSURE = """\
Signing in claims this unclaimed workspace: its key and the traces sent so
far are attached to a Rius workspace your account can access (a new account
gets a `Default` workspace). The stored key stays the same."""

ENV_KEY_BLOCKS_CLAIM = """\
RIUS_API_KEY is set, so it is the key in use and this command will not claim
or replace the unclaimed workspace stored in ~/.claude/rius/credentials.json.
Unset RIUS_API_KEY and run it again, or claim in a browser: %s"""

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
    # A bare `/rius` arrives as just the flags: no action means `status`.
    has_action = bool(argv) and not argv[0].startswith("--")
    action = argv[0] if has_action else "status"
    session_id = None
    cwd = None
    positional = []
    i = 1 if has_action else 0
    while i < len(argv):
        if argv[i] == "--session" and i + 1 < len(argv):
            session_id = argv[i + 1]
            i += 2
        elif argv[i] == "--cwd" and i + 1 < len(argv):
            cwd = argv[i + 1]
            i += 2
        else:
            if not argv[i].startswith("--"):
                positional.append(argv[i])
            i += 1
    return action, session_id, cwd, positional


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
    if cfg.claim_url:
        print("Workspace: unclaimed. Claim: %s" % cfg.claim_url)
    elif cfg.workspace_name:
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


def _env_key_set():
    return bool(os.environ.get("RIUS_API_KEY"))


def _stored_unclaimed(home):
    creds = login.read_credentials(home)
    return creds if anonymous.is_unclaimed(creds) else None


def _refuse_claim_over_env_key(unclaimed):
    if unclaimed and _env_key_set():
        print(ENV_KEY_BLOCKS_CLAIM % unclaimed["claim_url"])
        return True
    return False


def _login(home, _args=()):
    unclaimed = _stored_unclaimed(home)
    if _refuse_claim_over_env_key(unclaimed):
        return
    print(CLAIM_DISCLOSURE if unclaimed else login.DISCLOSURE)
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


_STILL_WAITING = ("Still waiting for approval in the browser. "
                  "Run `/rius login-wait` again once you have approved.")


def _login_wait(home, _args=()):
    unclaimed = _stored_unclaimed(home)
    if _refuse_claim_over_env_key(unclaimed):
        return
    if unclaimed:
        _claim_after_login(home)
        return
    creds = login.wait(home)
    if creds is None:
        print(_STILL_WAITING)
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


def _claim_after_login(home):
    token = login.wait_for_token(home)
    if token is None:
        print(_STILL_WAITING)
        return
    creds, choices = anonymous.after_login(home, token)
    if creds:
        _print_claimed(creds)
        return
    print("Your account can access several workspaces. Choose where this "
          "one's key and traces should go:")
    print(anonymous.format_targets(choices))
    print("Run `/rius claim <number>` (a workspace name or id also works) "
          "to finish.")


def _print_claimed(creds):
    print("Claimed. Traces now go to workspace %s (%s); the ones sent before "
          "the claim are copied there in the background."
          % (creds.get("workspace_name") or creds.get("workspace_id"),
             creds.get("org_name") or "your org"))
    print("The key is unchanged, so there is nothing to reconfigure.")


def _claim(home, args):
    if _refuse_claim_over_env_key(_stored_unclaimed(home)):
        return
    if not args:
        print("Usage: /rius claim <number|name|id>, from the list "
              "`/rius login` printed.")
        return
    try:
        creds = anonymous.finish_claim(home, " ".join(args))
    except anonymous.SignInExpired:
        print("The sign-in for this claim has expired; starting a new one.")
        _login(home)
        return
    _print_claimed(creds)


def _print_provision_failure(exc):
    print("Rius could not provision a workspace: %s" % exc)
    print("Tracing stays off until a key exists: run this again, "
          "`/rius login`, or set RIUS_API_KEY.")


def _provision(home, _args=()):
    if _env_key_set():
        print("RIUS_API_KEY is set and is the key in use, so no workspace "
              "was provisioned.")
        return
    creds = login.read_credentials(home)
    if creds:
        print("A Rius key is already stored in %s; nothing was provisioned."
              % login.credentials_path(home))
        if anonymous.is_unclaimed(creds):
            print("Claim this workspace any time: %s" % creds["claim_url"])
        return
    try:
        creds = anonymous.provision(home)
    except login.LoginError as exc:
        _print_provision_failure(exc)
        return
    print("Provisioned an unclaimed Rius workspace; its key is in %s."
          % login.credentials_path(home))
    print("Claim this workspace any time: %s" % creds["claim_url"])


def _provision_if_keyless(home):
    if _env_key_set() or login.read_credentials(home):
        return
    try:
        creds = anonymous.provision(home)
    except login.LoginError as exc:
        _print_provision_failure(exc)
        return
    print("Tracing on. Claim this workspace any time: %s" % creds["claim_url"])


def _logout(home, _args=()):
    unclaimed = _stored_unclaimed(home)
    anonymous.forget_pending(home)
    if not login.clear_credentials(home):
        print("No stored Rius key to remove.")
        return
    print("Removed the stored Rius key. It is still valid on the server "
          "until it expires or is revoked in the console.")
    if unclaimed:
        print("WARNING: this workspace was never claimed. Its claim URL is "
              "now the only way back to its traces, so keep it: %s"
              % unclaimed["claim_url"])


def _run_account_action(action, home, args):
    handlers = {"login": _login, "login-wait": _login_wait, "logout": _logout,
                "provision": _provision, "claim": _claim}
    try:
        handlers[action](home, args)
    except login.LoginError as exc:
        print("Rius %s failed: %s" % (action, exc))


def dispatch(argv, home):
    action, session_id, cwd, args = _parse_args(argv)

    if action in ACCOUNT_ACTIONS:
        _run_account_action(action, home, args)
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
        _provision_if_keyless(home)
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
