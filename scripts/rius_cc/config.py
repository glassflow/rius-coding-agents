"""Configuration resolution for the Rius Claude Code plugin.

Default is OFF. Nothing is traced unless a folder is explicitly opted in
by some layer of the resolution ladder (see `resolve`). This module never
raises on a corrupt or missing config file -- it falls back to the safe
default instead.

The environment can only turn things OFF. It includes the env block of a
project's committed .claude/settings.json, so anything it could turn on, a
cloned repo could too: it cannot enable tracing, raise capture, or choose
the key or the server the key goes to.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
from typing import Mapping, Optional, Tuple

from . import login
from .platform_compat import IS_WINDOWS

DEFAULT_ENDPOINT = "https://ingest.eu.console.rius-glassflow.com"
# Must match the default in .mcp.json, which cannot read this module.
DEFAULT_MCP_URL = login.ENVIRONMENTS[login.DEFAULT_ENVIRONMENT]["mcp_url"]
DEFAULT_SERVICE_NAME = "claude-code"
DEFAULT_MAX_ATTR_BYTES = 32768

_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")
_GLOB_RE = re.compile(r"[*?\[]")

STORED_KEY_SOURCE = "/rius:login"

_NO_KEY = "no API key: run `/rius:login`"
_UNTRUSTED_ENDPOINT = ("no API key: the stored key's server %s is not a Rius "
                       "server, so it is never sent there; run `/rius:login`")

IGNORED_API_KEY = ("RIUS_API_KEY is set but ignored; run /rius:login, or store "
                   "a console key with `rius_ctl.sh use-key`")
IGNORED_ENDPOINT = ("RIUS_ENDPOINT is set but ignored; traces go to the "
                    "server your /rius:login key came from")
IGNORED_ENABLE = ("RIUS_CLAUDE_ENABLED=true is set but ignored; run "
                  "/rius:enable-here to trace a folder")

_TRUE_VALUES = {"true", "1"}
_FALSE_VALUES = {"false", "0"}


class Config:
    def __init__(self, enabled, reason, api_key, endpoint, service_name,
                 capture_content, max_attr_bytes, debug, key_source=None,
                 workspace_name=None, ignored_env=()):
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
        # One line per environment setting that asked for something only
        # the user's own config may grant.
        self.ignored_env = list(ignored_env)


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
    rule = _before_glob(rule)               # "/*" is as broad as "/"
    if rule.startswith("/") and not rule.startswith("//"):
        return bool(rule.rstrip("/"))       # "/" -> the whole filesystem
    if _DRIVE_RE.match(rule):
        return bool(rule[3:].strip("\\/"))  # "C:\" -> the whole drive
    if rule.startswith("\\\\") or rule.startswith("//"):
        # UNC needs a server AND a share: "\\srv" alone is not a location.
        parts = [p for p in rule.replace("/", "\\").split("\\") if p]
        return len(parts) >= 2
    return False                            # "", " ", "*", "**", "opt/proj"


def _before_glob(rule: str) -> str:
    match = _GLOB_RE.search(rule)
    return rule[:match.start()] if match else rule


def resolved(path: str) -> str:
    """The folder `path` names, spelled as Claude Code hands it to the hooks.

    The slash commands only see the shell's $PWD, which keeps symlinks and
    the letter case that was typed: on macOS `cd /tmp/ABC` is
    /private/tmp/abc to Claude Code. A rule is resolved once, when it is
    written, and never while matching: a rule that followed its symlink at
    match time would trace wherever that link is repointed to later, and
    would cost every hook a filesystem call. Windows is left as written.
    """
    if IS_WINDOWS or not path.startswith("/"):
        return path
    return _getcwd_spelling(os.path.realpath(path))


def _getcwd_spelling(folder: str) -> str:
    """getcwd() spells a folder the way Claude Code's process.cwd() does,
    letter case included; realpath keeps the case it was given."""
    try:
        here = os.getcwd()
    except OSError:
        return folder
    try:
        os.chdir(folder)
        return os.getcwd()
    except OSError:
        return folder
    finally:
        try:
            os.chdir(here)
        except OSError:
            pass


def resolved_rule(rule: str) -> str:
    """`rule` with the folder its glob sits in resolved."""
    head = _before_glob(rule)
    if head == rule:
        return resolved(rule)
    cut = head.rfind("/")
    if cut <= 0:
        return rule
    return resolved(rule[:cut]).rstrip("/") + rule[cut:]


def is_usable_rule(rule) -> bool:
    return _is_usable_rule(rule)


def same_folder(a: str, b: str) -> bool:
    return a == b or resolved(a) == resolved(b)


def symlinked_rules(cwd: str, home: str) -> list:
    """(rule, enables) for rules an earlier version wrote in the shell's
    spelling that cover `cwd` as the shell spells it. Hooks see the
    resolved folder, so they never match."""
    rules = read_path_rules(home)
    return [(rule, enables)
            for key, enables in (("disabled_paths", False),
                                 ("enabled_paths", True))
            for rule in rule_list(rules, key)
            if _is_usable_rule(rule) and not _GLOB_RE.search(rule)
            and resolved(rule) != rule and _rule_matches(cwd, rule)]


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


def disabled_below(cwd: str, home: str) -> list:
    """Disable rules strictly inside `cwd`, which enabling `cwd` does not
    reach: matching_rule only ever looks upward from a folder."""
    prefix = _normalise_for_match(cwd).rstrip("/") + "/"
    return [rule for rule in rule_list(read_path_rules(home), "disabled_paths")
            if _is_usable_rule(rule)
            and _normalise_for_match(rule).startswith(prefix)]


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
    """The environment may only lower the cap: raising it sends more."""
    try:
        wanted = int(env.get("RIUS_CLAUDE_MAX_ATTR_BYTES", ""))
    except (TypeError, ValueError):
        return DEFAULT_MAX_ATTR_BYTES
    return wanted if 0 < wanted < DEFAULT_MAX_ATTR_BYTES else DEFAULT_MAX_ATTR_BYTES


_SERVICE_NAME_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")


def _service_name(env: Mapping[str, str]) -> str:
    """Only relabels, but still never more than a short plain name."""
    wanted = env.get("RIUS_SERVICE_NAME", "")
    return wanted if _SERVICE_NAME_RE.fullmatch(wanted) else DEFAULT_SERVICE_NAME


def redact(api_key: Optional[str]) -> str:
    if not api_key:
        return "<unset>"
    idx = api_key.find("_")
    if idx == -1:
        return "<redacted>"
    return api_key[: idx + 1] + "…"


def key_fingerprint(api_key: Optional[str], endpoint: str) -> str:
    """A stable, non-secret name for the key and the endpoint it goes to.

    A hash rather than the part before the `.`: in a `gf_<random>.<sig>` key
    that part is most of the key. Truncated, so it identifies without being
    worth anything on its own.
    """
    if not api_key:
        return ""
    digest = hashlib.sha256(
        (endpoint.rstrip("/") + "\n" + api_key).encode("utf-8"))
    return digest.hexdigest()[:16]


def _credential(home: str):
    """(api_key, endpoint, source, workspace_name, problem).

    Only the key `/rius:login` stored, and only ever to the endpoint stored
    with it: a key minted on one environment is meaningless against
    another's ingest, and anywhere else it is a leak."""
    creds = login.read_credentials(home)
    if not creds:
        return None, DEFAULT_ENDPOINT, None, None, _NO_KEY
    endpoint = creds.get("endpoint")
    if not login.is_rius_url(endpoint, creds.get("env")):
        return None, DEFAULT_ENDPOINT, None, None, _UNTRUSTED_ENDPOINT % endpoint
    source = (login.USE_KEY_SOURCE if creds.get("source") == login.USE_KEY_SOURCE
              else STORED_KEY_SOURCE)
    return (creds["api_key"], endpoint, source, creds.get("workspace_name"), None)


def _ignored_env(env: Mapping[str, str]) -> list:
    notes = []
    if env.get("RIUS_API_KEY"):
        notes.append(IGNORED_API_KEY)
    if env.get("RIUS_ENDPOINT"):
        notes.append(IGNORED_ENDPOINT)
    if _parse_bool_env(env.get("RIUS_CLAUDE_ENABLED")) is True:
        notes.append(IGNORED_ENABLE)
    return notes


def mcp_url(env: Mapping[str, str]) -> str:
    """The URL the bundled MCP server connects to: .mcp.json expands
    RIUS_MCP_URL and nothing else, so the stored credential cannot steer it."""
    return env.get("RIUS_MCP_URL") or DEFAULT_MCP_URL


def misdirected_mcp_url(env: Mapping[str, str], home: str) -> Optional[str]:
    """The stored key's MCP URL when the bundled server points elsewhere, so
    `/mcp` would present that key to the wrong environment.

    RIUS_API_KEY does not enter into it: Claude Code runs a plugin's
    headersHelper without credential-named variables, so the bundled server
    only ever sees the stored key."""
    creds = login.read_credentials(home)
    if not creds:
        return None
    wanted = (creds.get("mcp_url")
              or login.ENVIRONMENTS.get(creds.get("env"), {}).get("mcp_url"))
    if not isinstance(wanted, str) or not wanted:
        return None
    if wanted.rstrip("/") == mcp_url(env).rstrip("/"):
        return None
    return wanted


def _enabled(session_id: str, cwd: str, env: Mapping[str, str], home: str):
    """(enabled, reason). A session's own /rius:on or /rius:off decides it;
    otherwise any disable wins, and only a path rule enables."""
    session_override = _read_session_override(session_id, home)
    if session_override is not None:
        return session_override, ("on: session override" if session_override
                                  else "off: session override")
    if _parse_bool_env(env.get("RIUS_CLAUDE_ENABLED")) is False:
        return False, "off: RIUS_CLAUDE_ENABLED"
    path_decision, path_reason = _path_rules_decision(cwd, home)
    if path_decision is not None:
        return path_decision, path_reason
    return False, "off: no path rule matches %s, and the default is off" % cwd


def resolve(session_id: str, cwd: str, env: Mapping[str, str], home: str) -> Config:
    api_key, endpoint, key_source, workspace_name, no_key = _credential(home)
    ignored_env = _ignored_env(env)
    service_name = _service_name(env)
    # The environment may only lower capture.
    capture_content = _parse_bool_env(env.get("RIUS_CAPTURE_CONTENT")) is not False
    max_attr_bytes = _max_attr_bytes(env)
    debug = bool(_parse_bool_env(env.get("RIUS_CLAUDE_DEBUG")))

    enabled, reason = _enabled(session_id, cwd, env, home)
    if not api_key:
        if enabled:
            enabled = False
            reason = "off: " + no_key
        else:
            reason = reason + "; also " + no_key

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
        ignored_env=ignored_env,
    )
