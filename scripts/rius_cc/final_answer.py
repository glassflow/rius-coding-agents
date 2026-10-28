"""Claude Code writes a turn's final answer to the transcript a moment after
the Stop hook fires, so the exporter for Stop waits for it, briefly. Without
the wait, the turn's last model call is sent at the next hook: minutes later,
or never.

A prompt typed while Claude worked can land in the same write as the answer.
It opens the next turn, so the wait ends there and Stop reads no further:
past it, Stop would close the new turn along with its own.

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


def note(st: dict, entries: List[Any]) -> None:
    """Record whether the latest message read so far awaits an answer."""
    st["unanswered"] = _unanswered(entries, st.get("unanswered", True))


def next_turn(entries: List[Any], st: dict, prompt_id: str) -> int:
    """The index in `entries` of the first prompt after the turn a Stop ends,
    else len(entries). `prompt_id` names that turn, as the Stop payload does.
    Without it, the next turn is the one prompted after the last answer: the
    open turns cannot say, since an interrupted turn stays open."""
    if not prompt_id:
        return _prompt_after_last_answer(entries)
    seen = prompt_id in (st.get("open_turns") or {})
    for i, entry in enumerate(entries):
        if not _is_prompt(entry):
            continue
        if entry.prompt_id == prompt_id:
            seen = True
        elif seen:
            return i
    return len(entries)


def wait(path: str, st: dict, prompt_id: str,
         clock=time.monotonic, sleep=time.sleep) -> bool:
    """Wait at most WAIT_S for the end of the turn a Stop ends to reach the
    transcript: an answer to its latest message, or the next turn's prompt.
    True once it has."""
    deadline = clock() + WAIT_S
    seen_size = -1
    while True:
        size = _size(path)
        if size < 0:
            return False
        if size != seen_size:
            seen_size = size
            entries, _ = transcript.read_from(path, st.get("offset", 0))
            if _turn_ended(entries, st, prompt_id):
                return True
        if clock() >= deadline:
            return False
        sleep(POLL_S)


def _turn_ended(entries: List[Any], st: dict, prompt_id: str) -> bool:
    cut = next_turn(entries, st, prompt_id)
    return (cut < len(entries)
            or not _unanswered(entries[:cut], st.get("unanswered", True)))


def _unanswered(entries: List[Any], before: bool) -> bool:
    """Whether the latest main-chain message is the user's (a prompt or tool
    results). `before` is that for what was read earlier."""
    for entry in reversed(entries):
        if entry.kind in transcript.CONVERSATIONAL and not entry.is_sidechain:
            return entry.kind == "user"
    return before


def _prompt_after_last_answer(entries: List[Any]) -> int:
    answers = [i for i, entry in enumerate(entries) if _is_answer(entry)]
    start = answers[-1] + 1 if answers else len(entries)
    return next((i for i in range(start, len(entries))
                 if _is_prompt(entries[i])), len(entries))


def _is_answer(entry: Any) -> bool:
    return entry.kind == "assistant" and not entry.is_sidechain


def _is_prompt(entry: Any) -> bool:
    return (entry.kind == "user" and not entry.is_sidechain
            and bool(entry.prompt_id) and not entry.tool_results()
            and not entry.is_compact_summary())


def _size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return -1
