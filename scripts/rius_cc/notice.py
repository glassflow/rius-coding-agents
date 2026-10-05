"""The one line a user sees from Rius when a session starts.

SessionStart may print a hook JSON object whose `systemMessage` Claude Code
shows to the user. That is the only thing this module produces: a status
line, never instructions for the model. A session sees each line once; a
resumed, cleared or compacted session sees it again only when the line has
changed, because the state it describes has.

Everything here reads small local files. No network: the hook that calls it
runs in the session's critical path.

Standard library only.
"""
from __future__ import annotations

import json
import os
import time
from typing import Optional

from . import config, login, state

TRACING = "Rius: tracing this session%s (content: %s)"
SIGN_IN = "Rius: run /rius:login to start tracing"
INSTALLED = "Rius installed: run /rius:login, then /rius:enable-here"
REFUSED = ("Rius: your workspace wasn't accepting data last time (trial "
           "ended or paused). If you've fixed it, this clears on the next "
           "upload.")

# The receiver answers 402 when billing stops a workspace's ingest: a trial
# that ran out, a locked or canceled organization, or a workspace paused for
# exceeding the plan (argus-core apps/receiver/internal/handler).
REFUSED_STATUS = 402

# A session's record of the line it was shown only has to outlive the gap
# before it is resumed.
SHOWN_RECORD_MAX_AGE_S = 30 * 24 * 60 * 60


def _rius_dir(home: str) -> str:
    return os.path.join(home, ".claude", "rius")


def _refused_path(home: str) -> str:
    return os.path.join(_rius_dir(home), "refused.json")


def _install_shown_path(home: str) -> str:
    return os.path.join(_rius_dir(home), "install-notice-shown")


def _shown_dir(home: str) -> str:
    return os.path.join(_rius_dir(home), "notices")


def _shown_path(session_id: str, home: str) -> str:
    return os.path.join(_shown_dir(home), session_id)


def record_refusal(home: str, cfg, status: int) -> None:
    """Remember that the backend refuses this key's data. Sticky across
    sessions until an export under the same key succeeds."""
    if status != REFUSED_STATUS:
        return
    try:
        login._write_private(_refused_path(home), {
            "status": status,
            "key_fingerprint": config.key_fingerprint(cfg.api_key,
                                                      cfg.endpoint),
        })
    except OSError:
        pass


def clear_refusal(home: str) -> None:
    try:
        os.remove(_refused_path(home))
    except OSError:
        pass


def is_refused(home: str, cfg) -> bool:
    """A refusal recorded for the key and endpoint `cfg` uses now: signing in
    to another workspace is a fresh start."""
    if not cfg.api_key:
        return False
    try:
        with open(_refused_path(home)) as fh:
            recorded = json.load(fh)
    except (OSError, ValueError):
        return False
    return (isinstance(recorded, dict) and recorded.get("key_fingerprint")
            == config.key_fingerprint(cfg.api_key, cfg.endpoint))


def _opted_in_without_key(cfg) -> bool:
    return not cfg.api_key and cfg.reason == "off: " + config._NO_KEY


def _tracing_line(cfg) -> str:
    where = " to workspace %s" % cfg.workspace_name if cfg.workspace_name else ""
    return TRACING % (where, "on" if cfg.capture_content else "off")


def current(cfg, home: str, stopped: bool = False) -> Optional[str]:
    """The line that describes this session's Rius state, or None for a user
    who has not opted in (they are told once, ever, by `_install_line`).

    `stopped` is the exporter's sticky stop: a session disabled after its
    trace started sends nothing more, whatever the rules now say."""
    if stopped:
        return None
    if cfg.enabled:
        return REFUSED if is_refused(home, cfg) else _tracing_line(cfg)
    if _opted_in_without_key(cfg):
        return SIGN_IN
    if not cfg.api_key:
        return _install_line(home)
    return None


def _install_line(home: str) -> Optional[str]:
    path = _install_shown_path(home)
    if os.path.exists(path):
        return None
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w"):
            pass
    except OSError:
        return None  # unrecordable means it would show every session
    return INSTALLED


def _last_shown(session_id: str, home: str) -> Optional[str]:
    try:
        with open(_shown_path(session_id, home)) as fh:
            return fh.read()
    except OSError:
        return None


def _remember_shown(session_id: str, home: str, line: str) -> None:
    path = _shown_path(session_id, home)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(line)
    except OSError:
        pass


def _shown_before(session_id: str, home: str, line: str) -> bool:
    """Shown already to this session, or to the one it continues: Claude
    Code moves a backgrounded conversation to a new id (continuation.py)."""
    if _last_shown(session_id, home) == line:
        return True
    predecessor = state.load(session_id, home).get("continued_from")
    return bool(predecessor) and _last_shown(predecessor, home) == line


def prune_shown(home: str, keep: str, now: Optional[float] = None) -> None:
    """Forget the lines shown to sessions untouched for a month."""
    cutoff = (time.time() if now is None else now) - SHOWN_RECORD_MAX_AGE_S
    directory = _shown_dir(home)
    try:
        names = os.listdir(directory)
    except OSError:
        return
    for name in names:
        if name == keep:
            continue
        path = os.path.join(directory, name)
        try:
            if os.path.getmtime(path) < cutoff:
                os.remove(path)
        except OSError:
            pass


def for_session_start(session_id: str, cfg, home: str) -> Optional[str]:
    """The hook JSON to print at SessionStart, or None to print nothing."""
    prune_shown(home, keep=session_id)
    stopped = bool(state.load(session_id, home).get("content_stopped"))
    line = current(cfg, home, stopped)
    if line is None or _shown_before(session_id, home, line):
        return None
    _remember_shown(session_id, home, line)
    return json.dumps({"systemMessage": line})
