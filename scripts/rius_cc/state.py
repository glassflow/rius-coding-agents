"""Per-session state persistence and file locking.

State is the small dict the span builder (spans.py) reads and mutates
across hook invocations: transcript offset, open tool/turn spans, and
root span bookkeeping. Saves are atomic (temp file + os.replace) and a
corrupt state file resets to a fresh state instead of raising.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile


def new_state() -> dict:
    return {
        "offset": 0,
        "open_tools": {},
        "open_turns": {},
        "root_started": False,
        "root_start_ns": 0,
        "last_ns": 0,
        "open_task_spans": [],
    }


def state_dir(home: str) -> str:
    d = os.path.join(home, ".claude", "rius", "state")
    os.makedirs(d, exist_ok=True)
    return d


def state_path(session_id: str, home: str) -> str:
    return os.path.join(state_dir(home), session_id + ".json")


def lock_path(session_id: str, home: str) -> str:
    return os.path.join(state_dir(home), session_id + ".lock")


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
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


@contextlib.contextmanager
def session_lock(session_id: str, home: str):
    path = lock_path(session_id, home)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield True
        except OSError:
            yield False
    finally:
        os.close(fd)
