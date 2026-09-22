#!/usr/bin/env python3
"""CLI backing the /rius slash command.

Actions: on | off | clear | enable-here | status
Flags:   --session <id>   --cwd <path>

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

from rius_cc import config, state  # noqa: E402

USAGE = (
    "Usage: rius_ctl.py <on|off|clear|enable-here|status> "
    "[--session <id>] [--cwd <path>]"
)


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


def _print_status(session_id, cwd, home):
    cfg = config.resolve(session_id or "", cwd or "", os.environ, home)
    print("Rius tracing: %s" % ("on" if cfg.enabled else "off"))
    print("Reason: %s" % cfg.reason)
    print("cwd: %s" % (cwd or "<unknown, default>"))
    print("session: %s" % (session_id or "<unknown>"))
    print("Endpoint: %s" % cfg.endpoint)
    print("API key: %s" % config.redact(cfg.api_key))
    spans = _spans_exported(session_id, home)
    if spans is not None:
        print("Spans exported this session: %s" % spans)
    else:
        print("Spans exported this session: unknown")


def main():
    try:
        home = os.path.expanduser("~")
        action, session_id, cwd = _parse_args(sys.argv[1:])

        if session_id is None:
            session_id = _most_recent_session(home)
            if session_id is None:
                session_id = ""

        if action == "on":
            config.set_session_override(session_id, home, True)
            print("Rius tracing turned on for this session.")
        elif action == "off":
            config.set_session_override(session_id, home, False)
            print("Rius tracing turned off for this session.")
        elif action == "clear":
            config.set_session_override(session_id, home, None)
            print("Session override cleared.")
        elif action == "enable-here":
            target_cwd = cwd or os.getcwd()
            _enable_here(target_cwd, home)
            print("Rius tracing enabled for %s." % target_cwd)
        elif action == "status":
            _print_status(session_id, cwd, home)
        else:
            print(USAGE)
    except BaseException as exc:  # never fail this CLI
        print("rius_ctl.py error: %s" % exc)
    sys.exit(0)


if __name__ == "__main__":
    main()
