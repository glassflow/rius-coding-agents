"""Configuration resolution for the Rius Claude Code plugin.

Default is OFF. Nothing is traced unless a folder is explicitly opted in
by some layer of the resolution ladder (see `resolve`). This module never
raises on a corrupt or missing config file -- it falls back to the safe
default instead.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
from typing import Mapping, Optional, Tuple

from . import login
from .platform_compat import IS_WINDOWS

DEFAULT_ENDPOINT = "https://ingest.eu.console.rius-glassflow.com"
DEFAULT_SERVICE_NAME = "claude-code"
DEFAULT_MAX_ATTR_BYTES = 32768

_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")

STORED_KEY_SOURCE = "/rius:login"

_NO_KEY = "no API key: run `/rius:login` (or set RIUS_API_KEY)"

_TRUE_VALUES = {"true", "1"}
_FALSE_VALUES = {"false", "0"}


class Config:
    def __init__(self, enabled, reason, api_key, endpoint, service_name,
                 capture_content, max_attr_bytes, debug, key_source=None,
                 workspace_name=None):
        self.enabled = enabled
        self.reason = reason
        self.api_key = api_key
        self.key_source = key_source
        self.workspace_name = workspace_name
        self.endpoint = endpoint
        self.service_name = service_name
        self.capture_content = capture_content
        self.max_attr_bytes = max_attr_bytes
        self.debug = debug


def _rius_dir(home: str) -> str:
    return os.path.join(home, ".claude", "rius")


def _sessions_dir(home: str) -> str:
    return os.path.join(_rius_dir(home), "sessions")


def session_override_path(session_id: str, home: str) -> str:
    return os.path.join(_sessions_dir(home), session_id)


def path_rules_path(home: str) -> str:
    return os.path.join(_rius_dir(home), "config.json")


def set_session_override(session_id: str, home: str, on: Optional[bool]) -> None:
    path = session_override_path(session_id, home)
    if on is None:
        try:
            os.remove(path)
        except OSError:
            pass
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write("on" if on else "off")


def _read_session_override(session_id: str, home: str) -> Optional[bool]:
    path = session_override_path(session_id, home)
    try:
        with open(path) as fh:
            content = fh.read().strip().lower()
    except OSError:
        return None
    if content == "on":
        return True
    if content == "off":
        return False
    return None


def _parse_bool_env(value: Optional[str]) -> Optional[bool]:
    if value is None:
        return None
    lowered = value.strip().lower()
    if lowered in _TRUE_VALUES:
        return True
    if lowered in _FALSE_VALUES:
        return False
    return None


def read_path_rules(home: str) -> dict:
    path = path_rules_path(home)
    try:
        with open(path) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def write_path_rules(home: str, rules: dict) -> None:
    login._write_private(path_rules_path(home), rules)


def rule_list(rules: dict, key: str) -> list:
    value = rules.get(key)
    return list(value) if isinstance(value, list) else []


def _is_usable_rule(rule) -> bool:
    r"""Reject rules that would match (almost) every path on the machine.

    `cwd.startswith(rule.rstrip("/") + "/")` is true for EVERY absolute path
    when rule is "" or "/", and fnmatch turns "*" into the same thing. A
    hand-edited or truncated config.json with one stray entry would otherwise
    silently enable tracing -- with full content capture -- for the whole
    filesystem. A rule has to be an absolute path naming something below the
    root to be worth honouring; anything else is a typo, not an intent.

    "Absolute" is platform-shaped. On Windows it is `C:\proj` or
    `\\server\share\proj`; the POSIX-only leading-"/" test rejected every
    rule `/rius:enable-here` had just written there, so tracing could never
    be turned on and nothing said why. The degenerate cases are rejected in
    the Windows spelling too: a bare drive root (`C:\`) is as broad as "/".
    """
    if not isinstance(rule, str) or not rule:
        return False
    if rule.startswith("/") and not rule.startswith("//"):
        return bool(rule.rstrip("/"))       # "/" -> the whole filesystem
    if _DRIVE_RE.match(rule):
        return bool(rule[3:].strip("\\/"))  # "C:\" -> the whole drive
    if rule.startswith("\\\\") or rule.startswith("//"):
        # UNC needs a server AND a share: "\\srv" alone is not a location.
        parts = [p for p in rule.replace("/", "\\").split("\\") if p]
        return len(parts) >= 2
    return False                            # "", " ", "*", "**", "opt/proj"


def _normalise_for_match(path: str) -> str:
    r"""Compare paths the way the local filesystem would.

    A no-op on POSIX. On Windows the separator is either slash and the
    comparison is case-insensitive, so `C:\Proj` and `c:/proj` are one
    path -- treating them as two means a rule the user just wrote silently
    fails to match. This only ever merges spellings the OS itself already
    considers identical, so it cannot widen a rule beyond its own directory.
    """
    if not IS_WINDOWS:
        return path
    return path.replace("\\", "/").lower()


def _rule_matches(cwd: str, rule: str) -> bool:
    if not _is_usable_rule(rule):
        return False
    cwd = _normalise_for_match(cwd)
    rule = _normalise_for_match(rule)
    if cwd == rule:
        return True
    if cwd.startswith(rule.rstrip("/") + "/"):
        return True
    if fnmatch.fnmatch(cwd, rule):
        return True
    return False


def matching_rule(cwd: str, home: str) -> Optional[Tuple[str, bool]]:
    """(rule, enables) for the path rule that decides `cwd`, or None.
    Any matching disable beats every enable."""
    rules = read_path_rules(home)
    for key, enables in (("disabled_paths", False), ("enabled_paths", True)):
        for rule in rule_list(rules, key):
            if _rule_matches(cwd, rule):
                return rule, enables
    return None


def _path_rules_decision(cwd: str, home: str):
    match = matching_rule(cwd, home)
    if match is None:
        return None, None
    rule, enables = match
    verb = "enables" if enables else "disables"
    return enables, "%s: path rule %r %s %s" % ("on" if enables else "off",
                                                 rule, verb, cwd)


def _max_attr_bytes(env: Mapping[str, str]) -> int:
    raw = env.get("RIUS_CLAUDE_MAX_ATTR_BYTES")
    if raw is None:
        return DEFAULT_MAX_ATTR_BYTES
    try:
        return int(raw)
    except (TypeError, ValueError):
        return DEFAULT_MAX_ATTR_BYTES


def redact(api_key: Optional[str]) -> str:
    if not api_key:
        return "<unset>"
    idx = api_key.find("_")
    if idx == -1:
        return "<redacted>"
    return api_key[: idx + 1] + "…"


def _credential(env: Mapping[str, str], home: str):
    """(api_key, endpoint, source, workspace_name).

    RIUS_API_KEY wins over the file `/rius:login` writes: existing installs
    are configured that way. The stored endpoint travels with the stored key
    and only with it -- a key minted on one environment is meaningless
    against another's ingest."""
    if env.get("RIUS_API_KEY"):
        return (env["RIUS_API_KEY"], env.get("RIUS_ENDPOINT", DEFAULT_ENDPOINT),
                "RIUS_API_KEY", None)
    creds = login.read_credentials(home)
    if creds:
        endpoint = env.get("RIUS_ENDPOINT") or creds.get("endpoint") or DEFAULT_ENDPOINT
        return (creds["api_key"], endpoint, STORED_KEY_SOURCE,
                creds.get("workspace_name"))
    return None, env.get("RIUS_ENDPOINT", DEFAULT_ENDPOINT), None, None


def resolve(session_id: str, cwd: str, env: Mapping[str, str], home: str) -> Config:
    api_key, endpoint, key_source, workspace_name = _credential(env, home)
    service_name = env.get("RIUS_SERVICE_NAME", DEFAULT_SERVICE_NAME)
    capture_content = _parse_bool_env(env.get("RIUS_CAPTURE_CONTENT"))
    if capture_content is None:
        capture_content = True
    max_attr_bytes = _max_attr_bytes(env)
    debug = bool(_parse_bool_env(env.get("RIUS_CLAUDE_DEBUG")))

    enabled = False
    reason = "off: no path rule matches %s, and the default is off" % cwd

    session_override = _read_session_override(session_id, home)
    if session_override is not None:
        enabled = session_override
        reason = "on: session override" if enabled else "off: session override"
    else:
        env_decision = _parse_bool_env(env.get("RIUS_CLAUDE_ENABLED"))
        if env_decision is not None:
            enabled = env_decision
            reason = ("on: RIUS_CLAUDE_ENABLED" if enabled
                       else "off: RIUS_CLAUDE_ENABLED")
        else:
            path_decision, path_reason = _path_rules_decision(cwd, home)
            if path_decision is not None:
                enabled = path_decision
                reason = path_reason

    if not api_key:
        if enabled:
            enabled = False
            reason = "off: " + _NO_KEY
        else:
            reason = reason + "; also " + _NO_KEY

    return Config(
        enabled=enabled,
        reason=reason,
        api_key=api_key,
        endpoint=endpoint,
        service_name=service_name,
        capture_content=capture_content,
        max_attr_bytes=max_attr_bytes,
        debug=debug,
        key_source=key_source,
        workspace_name=workspace_name,
    )
