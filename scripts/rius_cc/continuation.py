"""A conversation Claude Code moved to a new session id.

When Claude Code 2.1.x sends a session to the background, its daemon starts
the conversation under a NEW session id. It appends one record to the old
transcript,

    {"type": "continued-in", "sessionId": <old>, "continuedInSessionId": <new>,
     "timestamp": ...}

and writes a new transcript, in the same project directory, that opens with
a copy of the whole history: the same entry uuids, a new sessionId and new
promptIds. The old process gets no SessionEnd.

The link is Claude Code's own record, matched exactly; nothing is inferred
from uuids alone. Once it is found, the new session skips the copied entries
(the old session already sent them) until the first entry the old
transcript does not have.

Standard library only.
"""
from __future__ import annotations

import json
import os
from typing import Any, List, Optional, Set, Tuple

from . import transcript

RECORD = "continued-in"

# The record is the last thing the old process writes, so the tail of its
# file is enough; and the old transcript is among the most recently written
# in the directory, so only the newest few are looked at.
TAIL_BYTES = 64 * 1024
MAX_CANDIDATES = 50


def _tail_lines(path: str) -> List[str]:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - TAIL_BYTES))
            data = fh.read()
    except OSError:
        return []
    return data.decode("utf-8", "replace").splitlines()


def find_predecessor(transcript_path: str, session_id: str
                     ) -> Optional[Tuple[str, str, int]]:
    """(old session id, old transcript path, switch time ns), or None.

    The switch time is the record's timestamp, 0 if it has none.
    """
    directory = os.path.dirname(transcript_path)
    own = os.path.basename(transcript_path)
    candidates = []
    try:
        names = os.listdir(directory)
    except OSError:
        return None
    for name in names:
        if not name.endswith(".jsonl") or name == own:
            continue
        path = os.path.join(directory, name)
        try:
            candidates.append((os.path.getmtime(path), path))
        except OSError:
            continue
    candidates.sort(reverse=True)
    for _, path in candidates[:MAX_CANDIDATES]:
        for line in reversed(_tail_lines(path)):
            if RECORD not in line or session_id not in line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if (isinstance(record, dict) and record.get("type") == RECORD
                    and record.get("continuedInSessionId") == session_id):
                old_id = record.get("sessionId") or os.path.basename(path)[:-len(".jsonl")]
                switch_ns = 0
                try:
                    switch_ns = transcript._timestamp_ns(record["timestamp"])
                except (KeyError, ValueError, TypeError, IndexError):
                    pass
                return old_id, path, switch_ns
    return None


def copied_uuids(path: str) -> Set[str]:
    """The uuids of the conversational entries in the old transcript."""
    entries, _ = transcript.read_from(path, 0)
    return {e.uuid for e in entries if e.uuid}


def drop_copied(entries: List[Any], uuids: Set[str]) -> Tuple[List[Any], bool]:
    """(entries without the copied prefix, whether the prefix has ended)."""
    kept: List[Any] = []
    ended = False
    for entry in entries:
        if not ended and entry.uuid in uuids:
            continue
        ended = True
        kept.append(entry)
    return kept, ended
