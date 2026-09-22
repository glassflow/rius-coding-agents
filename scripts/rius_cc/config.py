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
from typing import Mapping, Optional

DEFAULT_ENDPOINT = "https://ingest.eu.console.rius-glassflow.com"
DEFAULT_SERVICE_NAME = "claude-code"
DEFAULT_MAX_ATTR_BYTES = 32768

_TRUE_VALUES = {"true", "1"}
_FALSE_VALUES = {"false", "0"}


class Config:
    def __init__(self, enabled, reason, api_key, endpoint, service_name,
                 capture_content, max_attr_bytes, debug):
        self.enabled = enabled
        self.reason = reason
        self.api_key = api_key
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


def _read_path_rules(home: str) -> dict:
    path = path_rules_path(home)
    try:
        with open(path) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def _rule_matches(cwd: str, rule: str) -> bool:
    if cwd == rule:
        return True
    if cwd.startswith(rule.rstrip("/") + "/"):
        return True
    if fnmatch.fnmatch(cwd, rule):
        return True
    return False


def _path_rules_decision(cwd: str, home: str):
    rules = _read_path_rules(home)
    disabled_paths = rules.get("disabled_paths") or []
    enabled_paths = rules.get("enabled_paths") or []

    for rule in disabled_paths:
        if isinstance(rule, str) and _rule_matches(cwd, rule):
            return False, "off: path rule %r disables %s" % (rule, cwd)

    for rule in enabled_paths:
        if isinstance(rule, str) and _rule_matches(cwd, rule):
            return True, "on: path rule %r enables %s" % (rule, cwd)

    return None, None


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


def resolve(session_id: str, cwd: str, env: Mapping[str, str], home: str) -> Config:
    api_key = env.get("RIUS_API_KEY")
    endpoint = env.get("RIUS_ENDPOINT", DEFAULT_ENDPOINT)
    service_name = env.get("RIUS_SERVICE_NAME", DEFAULT_SERVICE_NAME)
    capture_content = _parse_bool_env(env.get("RIUS_CAPTURE_CONTENT"))
    if capture_content is None:
        capture_content = True
    max_attr_bytes = _max_attr_bytes(env)
    debug = bool(_parse_bool_env(env.get("RIUS_DEBUG")))

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
            reason = "off: RIUS_API_KEY is not set"
        else:
            reason = reason + "; also RIUS_API_KEY is not set"

    return Config(
        enabled=enabled,
        reason=reason,
        api_key=api_key,
        endpoint=endpoint,
        service_name=service_name,
        capture_content=capture_content,
        max_attr_bytes=max_attr_bytes,
        debug=debug,
    )
