#!/usr/bin/env python3
"""CLI backing the /rius:* slash commands (one command file per action).

Actions: on | off | clear | enable-here | content-on-here | disable-here |
         status | login | login-wait | logout | use-key
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
    "<on|off|clear|enable-here|content-on-here|disable-here|status|login|"
    "login-wait|logout|use-key> "
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


def _move_path(home, cwd, to_key, from_key, with_content=None):
    """Any other spelling of `cwd` goes, from both lists: a rule written
    through a symlink before 0.4.4 never matched what the hooks see.
    An enable records whether the folder sends content."""
    rules = config.read_path_rules(home)
    rules[from_key] = [p for p in config.rule_list(rules, from_key)
                       if not config.same_folder(p, cwd)]
    target = [p for p in config.rule_list(rules, to_key)
              if p == cwd or not config.same_folder(p, cwd)]
    if cwd not in target:
        target.append(cwd)
    rules[to_key] = target
    rules["disabled_paths"] = _resolved_disables(rules["disabled_paths"])
    choices = _content_choices(rules, cwd, with_content)
    rules.pop(config.CONTENT_CHOICES_KEY, None)
    if choices:
        rules[config.CONTENT_CHOICES_KEY] = choices
    config.write_path_rules(home, rules)


def _content_choices(rules, cwd, with_content):
    """Choices for the enable rules that are left, plus this one's."""
    enabled = config.rule_list(rules, "enabled_paths")
    choices = {rule: config.content_choice(rules, rule) for rule in enabled}
    if with_content is not None:
        choices[cwd] = with_content
    return {rule: choice for rule, choice in choices.items()
            if choice is not None}


def _resolved_disables(disables):
    """A disable an earlier version wrote through a symlink matches nothing
    the hooks see. Resolving it can only turn tracing off, so every write
    repairs them all: otherwise enabling its parent again would trace the
    folder the user had carved out."""
    repaired = []
    for rule in disables:
        rule = config.resolved_rule(rule)
        if rule not in repaired:
            repaired.append(rule)
    return repaired


TOO_BROAD = ("Not changed: `%s` is too broad for a rule (the filesystem or a "
             "drive root, or a *, ? or [ right below it).")


CONTENT_FLAG_REFUSED = ("Not changed: /rius:enable-here only sends structure. "
                        "To send content from this folder, run "
                        "/rius:enable-content-here.")


def _enable_here_structure_only(argv, cwd, home):
    """Content is a separate subcommand, so a grant for this one can never
    reach it: any other flag is refused rather than interpreted."""
    flags = [arg for arg in argv if arg.startswith("-")]
    if any(flag not in ("--cwd", "--session") for flag in flags):
        print(CONTENT_FLAG_REFUSED)
        return
    _enable_here(cwd, home, False)


def _enable_here(cwd, home, with_content):
    cwd = config.resolved(cwd)
    if not config.is_usable_rule(cwd):
        print(TOO_BROAD % cwd)
        return
    _move_path(home, cwd, "enabled_paths", "disabled_paths", with_content)
    match = config.matching_rule(cwd, home)
    if match and not match[1]:
        print(STILL_OFF % match[0])
    else:
        print(_enabled_disclosure(cwd, home, with_content))


SENDS_CONTENT = ("Sessions here %s prompts, replies, the contents of files "
                 "Claude reads and command output to %s, with the secrets "
                 "Rius recognises removed first.")
SENDS_STRUCTURE = ("Sessions here %s structure only (models, tokens, timing, "
                   "tool names) to %s: no prompts, replies, file contents or "
                   "command output.")
STOP_WITH_CONTENT = ("Run /rius:enable-here to send structure only, or "
                     "/rius:disable-here to stop.")
STOP_WITHOUT_CONTENT = ("Structure only is recommended. To include content "
                        "for this folder, run /rius:enable-content-here. "
                        "Run /rius:disable-here to stop.")
ENV_KEEPS_CONTENT_OFF = ("RIUS_CAPTURE_CONTENT=false in your environment keeps "
                         "content off here.")


def _enabled_disclosure(cwd, home, with_content):
    """RIUS-969: enabling a folder is the consent act, so say what it will
    upload, where to, and how to stop -- and only what is true right now."""
    cfg = config.resolve("", cwd, os.environ, home)
    if cfg.api_key:
        verb, dest = "now send", cfg.workspace_name or "your Rius workspace"
    else:
        verb = "will send"
        dest = "the workspace you pick once you sign in with /rius:login"
    if cfg.capture_content:
        lines = [SENDS_CONTENT % (verb, dest), STOP_WITH_CONTENT]
    elif with_content:
        lines = [SENDS_STRUCTURE % (verb, dest), ENV_KEEPS_CONTENT_OFF]
    else:
        lines = [SENDS_STRUCTURE % (verb, dest), STOP_WITHOUT_CONTENT]
    return "\n".join([_enabled_scope(cwd, home)] + lines)


def _enabled_scope(cwd, home):
    carved_out = config.disabled_below(cwd, home)
    if not carved_out:
        return "Rius tracing enabled for %s and everything under it." % cwd
    return ("Rius tracing enabled for %s and everything under it, except %s "
            "(disabled)." % (cwd, _and_list(carved_out)))


def _and_list(items):
    return items[0] if len(items) == 1 else "%s and %s" % (", ".join(items[:-1]),
                                                           items[-1])


def _disable_here(cwd, home):
    cwd = config.resolved(cwd)
    if not config.is_usable_rule(cwd):
        print(TOO_BROAD % cwd)
        return
    _move_path(home, cwd, "disabled_paths", "enabled_paths")
    print("Rius tracing disabled for %s." % cwd)


def _spans_exported(session_id, home):
    if not session_id:
        return None
    st = state.load(session_id, home)
    return st.get("spans_exported")


STOPPED_NOTE = ("Stopped: this session stopped tracing when its folder was "
                "disabled, and stays stopped even if the folder is enabled "
                "again. New sessions in this folder are traced as usual.")


def _print_status(session_id, cwd, home, inferred=False):
    # Decide for the folder the hooks are handed, not the shell's spelling.
    typed_cwd = cwd
    cwd = config.resolved(cwd) if cwd else cwd
    cfg = config.resolve(session_id or "", cwd or "", os.environ, home)
    # The exporter's sticky stop outranks the rules for this one session, so
    # without this the rules would say "on" for a session that sends nothing.
    stopped = bool(session_id) and state.load(session_id, home).get("content_stopped")
    print("Rius tracing: %s" % ("on" if cfg.enabled and not stopped else "off"))
    print("Reason: %s" % cfg.reason)
    for note in cfg.ignored_env:
        print(note)
    if stopped:
        print(STOPPED_NOTE)
    print(_cwd_line(typed_cwd))
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
    print(_mcp_key_line(home))
    if login.read_credentials(home):
        print(_mcp_hint(home, " with this key"))
    print("API key: %s" % config.redact(cfg.api_key))
    if cfg.key_source:
        print("Key from: %s" % cfg.key_source)
    _print_rule(cwd, home)
    print(_content_line(cfg))
    if cfg.unchosen_rule:
        print(UNCHOSEN_RULE % cfg.unchosen_rule)
    for rule, enables in (config.symlinked_rules(typed_cwd, home)
                          if typed_cwd else []):
        verb = "enable" if enables else "disable"
        print(STALE_RULE % (rule, verb, config.resolved(rule), verb, rule))
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
    if (cfg.enabled and not stopped and session_id
            and not os.path.exists(state.state_path(session_id, home))):
        print(NO_HOOK_RAN)


CONTENT_ON = "Content: prompts, replies, file contents and command output"
CONTENT_OFF = "Content: none (structure only)"
UNCHOSEN_RULE = ("Rule `%s` predates the content choice, so it still sends "
                 "content. Pick one: /rius:enable-here (structure only, "
                 "recommended) or /rius:enable-content-here.")


def _content_line(cfg):
    return CONTENT_ON if cfg.capture_content else CONTENT_OFF


STALE_RULE = ("Rule `%s` %ss nothing: Claude Code calls that folder %s. "
              "Run /rius:%s-here in %s to fix it.")
# Every hook in an enabled folder leaves this session's state file, a
# failed export included. A folder that is on with no state file is a
# session whose hooks never loaded, or have not fired since the enable.
NO_HOOK_RAN = ("No Rius hook has traced this session yet. If that is still "
               "so after your next prompt, restart Claude Code: a plugin "
               "installed after a session started is not hooked into it.")


def _cwd_line(cwd):
    if not cwd:
        return "cwd: <unknown, default>"
    hooks_see = config.resolved(cwd)
    if hooks_see == cwd:
        return "cwd: %s" % cwd
    return "cwd: %s (hooks see %s)" % (cwd, hooks_see)


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


def _mcp_key_line(home):
    if login.read_credentials(home):
        return "MCP key: /rius:login"
    return "MCP key: none; run /rius:login to query your traces."


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


def _use_key(home, env_flag):
    env_name = env_flag or login.DEFAULT_ENVIRONMENT
    creds = login.use_key(home, sys.stdin.read().strip(), env_name)
    print("Stored the key for %s; it is sent only to %s. Run /rius:enable-here "
          "in a folder to trace it." % (env_name, creds["endpoint"]))


def _date(timestamp):
    return timestamp[:10] if isinstance(timestamp, str) else "unknown"


def _run_account_action(action, home, cwd, env_flag):
    cwd = cwd or os.getcwd()
    handlers = {"login": lambda: _login(home, cwd, env_flag),
                "login-wait": lambda: _login_wait(home, cwd),
                "logout": lambda: _logout(home, cwd),
                "use-key": lambda: _use_key(home, env_flag)}
    try:
        handlers[action]()
    except (login.WaitInProgress, login.Superseded) as exc:
        print(exc)
    except login.LoginError as exc:
        print("Rius login failed: %s" % exc)


def dispatch(argv, home):
    action, session_id, cwd, env_flag = _parse_args(argv)

    if action in ("login", "login-wait", "logout", "use-key"):
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
        _enable_here_structure_only(argv, cwd or os.getcwd(), home)
    elif action == "content-on-here":
        _enable_here(cwd or os.getcwd(), home, True)
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
