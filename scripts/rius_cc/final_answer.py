"""Claude Code writes a turn's final answer to the transcript a moment after
the Stop hook fires, so the exporter for Stop waits for it, briefly. Without
the wait, the turn's last model call is sent at the next hook: minutes later,
or never.

Standard library only.
"""
from __future__ import annotations

import os
import time
from typing import Any, List

from . import transcript

# Claude Code flushes its transcript every 100 ms; this leaves room for a
# busy machine and still bounds a Stop whose answer never comes.
WAIT_S = 2.0
POLL_S = 0.05


def unanswered(entries: List[Any], before: bool) -> bool:
    """Whether the latest main-chain message is the user's (a prompt or tool
    results), which the model has yet to answer. `before` holds for the part
    of the transcript read earlier, and is the answer when `entries` hold no
    message."""
    for entry in reversed(entries):
        if entry.kind in transcript.CONVERSATIONAL and not entry.is_sidechain:
            return entry.kind == "user"
    return before


def wait(path: str, offset: int, unanswered_before: bool,
         clock=time.monotonic, sleep=time.sleep) -> bool:
    """Wait at most WAIT_S for the transcript past `offset` to answer its
    latest message. True once it does."""
    deadline = clock() + WAIT_S
    seen_size = -1
    while True:
        size = _size(path)
        if size < 0:
            return False
        if size != seen_size:
            seen_size = size
            entries, _ = transcript.read_from(path, offset)
            if not unanswered(entries, unanswered_before):
                return True
        if clock() >= deadline:
            return False
        sleep(POLL_S)


def _size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return -1
