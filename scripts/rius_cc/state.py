"""Per-session state persistence and file locking.

State is the small dict the span builder (spans.py) reads and mutates
across hook invocations: transcript offset, open tool/turn spans, and
root span bookkeeping. Saves are atomic (temp file + os.replace) and a
corrupt state file resets to a fresh state instead of raising.

Nothing here imports a platform-specific module: `fcntl` at module scope is
exactly what made the detached exporter die on import under Windows, with
its stderr on DEVNULL and nothing anywhere saying so.
"""
from __future__ import annotations

import contextlib
import json
import os
import tempfile
import time

from . import agent, platform_compat

RETRY_INTERVAL_S = 0.05
OPEN_MARKER_SUFFIX = ".open"


def new_state() -> dict:
    return {
        "offset": 0,
        "open_tools": {},
        "open_turns": {},
        "root_started": False,
        "root_start_ns": 0,
        "last_ns": 0,
        "open_task_spans": [],
        # The API response whose lines are still arriving (ids and start
        # only; see spans._generation_for).
        "open_gen": None,
        # Running prompt-size account (context_sizes.py): sizes, no content.
        "context": None,
        # Subagent drilldown. A subagent's transcript is a separate file, so
        # each one needs its own byte offset and its own span bookkeeping,
        # keyed by agent id; sub_links maps the spawning tool_use id to the
        # tool span its work hangs under. All plain JSON -- this dict is
        # written to disk between hook invocations.
        "sub_links": {},
        "sub_offsets": {},
        "sub_scopes": {},
        "spans_exported": 0,
        # Diagnostics. These exist so that "nothing was exported" is never
        # the whole story /rius:status can tell.
        "lines_skipped": 0,
        "consecutive_export_failures": 0,
        "last_export_error": None,
    }


def state_dir(home: str) -> str:
    d = agent.active().state_dir(home)
    os.makedirs(d, exist_ok=True)
    return d


def state_path(session_id: str, home: str) -> str:
    return os.path.join(state_dir(home), session_id + ".json")


def lock_path(session_id: str, home: str) -> str:
    return os.path.join(state_dir(home), session_id + ".lock")


def open_marker_path(session_id: str, home: str) -> str:
    return os.path.join(state_dir(home), session_id + OPEN_MARKER_SUFFIX)


def sync_open_marker(session_id: str, home: str, st: dict) -> None:
    """Mark the session as having an open trace, or clear the mark.

    Called once an export has succeeded, so a marker means the backend holds
    a pending root. The next SessionStart looks for these to close the trace
    of a session that was killed before its SessionEnd (exporter.sweep_stale).
    """
    path = open_marker_path(session_id, home)
    try:
        if trace_is_open(st) and not st.get("handed_off_to"):
            if not os.path.exists(path):
                with open(path, "w"):
                    pass
        else:
            os.remove(path)
    except OSError:
        pass


def open_marked_sessions(home: str) -> list:
    try:
        names = os.listdir(state_dir(home))
    except OSError:
        return []
    return sorted(name[:-len(OPEN_MARKER_SUFFIX)] for name in names
                  if name.endswith(OPEN_MARKER_SUFFIX))


def pinger_alive(session_id: str, home: str) -> bool:
    """Is the session's heartbeat pinger (heartbeat.py) still running?

    It exits when the Claude Code process it watches dies, so a live one
    means the session may well be alive too.
    """
    try:
        with open(os.path.join(state_dir(home),
                               session_id + ".heartbeat.pid")) as fh:
            pid = int(fh.read().strip())
    except (OSError, ValueError):
        return False
    return platform_compat.pid_alive(pid)


def trace_is_open(st: dict) -> bool:
    """The session's root span was sent and no SessionEnd has closed it."""
    return bool(st.get("root_started")) and not st.get("finalized")


def load(session_id: str, home: str) -> dict:
    path = state_path(session_id, home)
    try:
        with open(path) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return new_state()
    if not isinstance(data, dict):
        return new_state()
    defaults = new_state()
    for key, value in defaults.items():
        if key not in data:
            data[key] = value
    return data


def save(session_id: str, home: str, state: dict) -> None:
    path = state_path(session_id, home)
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=session_id, suffix=".tmp", dir=d)
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(state, fh)
        platform_compat.replace_atomic(tmp_path, path)
    except BaseException:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


@contextlib.contextmanager
def session_lock(session_id: str, home: str, block_timeout: float = 0.0,
                 clock=time.monotonic, sleep=time.sleep):
    """Yield True if the per-session lock was acquired, False otherwise.

    Non-blocking by default: for a mid-session event a later hook re-reads the
    same transcript lines, so losing the lock is free. `block_timeout` gives
    callers whose event is the LAST one for the session (SessionEnd, Stop) a
    bounded wait -- if they lose the lock, nothing ever finalises the session
    and the root span stays pending forever.

    The descriptor is opened fresh every call and closed in `finally`: both
    backing implementations (flock, and msvcrt byte-range locking) are
    per-descriptor, so a cached fd would make a process invisible to its own
    lock.

    The yield deliberately sits OUTSIDE the try/except: an OSError raised
    inside the caller's `with` body propagates back into this generator, and
    a yield in the handler would make it resume and yield a second time,
    raising RuntimeError("generator didn't stop after throw()") and masking
    the real error.
    """
    path = lock_path(session_id, home)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = platform_compat.open_lock_file(path)
    acquired = False
    try:
        deadline = clock() + max(0.0, block_timeout)
        while True:
            if platform_compat.try_lock(fd):
                acquired = True
                break
            if clock() >= deadline:
                break
            sleep(RETRY_INTERVAL_S)
        yield acquired
    finally:
        if acquired:
            platform_compat.unlock(fd)
        os.close(fd)
