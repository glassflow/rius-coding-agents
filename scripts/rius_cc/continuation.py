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
from uuids alone.

The rule is the resume rule: the same conversation continuing stays ONE
trace. So at the switch the new id takes the conversation over (link()):
the conversation's id -- whose trace and root every span keeps -- the root's
bookkeeping, the open turn and tools, the stop flag and the prompt-size
account all move into the new id's state, and the old id is marked handed
off. From then on the new id owns closing the root; the old id never does.
The old id's subagents keep their bookkeeping in its state, and the new id
keeps reading them (see exporter). The copied history the old id already
sent is skipped by the new id; anything the old process wrote but never
sent is sent once, by the new id.

Standard library only.
"""
from __future__ import annotations

import json
import os
from typing import Any, List, Optional, Set, Tuple

from . import state as state_mod
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


def copied_uuids(path: str, until: int = -1) -> Set[str]:
    """The uuids of the old transcript's entries the old session already
    sent: those before its transcript offset (`until`; -1 = all of them)."""
    entries, _ = transcript.read_from(path, 0)
    return {e.uuid for e in entries
            if e.uuid and (until < 0 or e.offset < until)}


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


# What moves from the old id's state to the new one at the switch: the root
# (so the new id neither opens a second root nor forgets to close this one),
# the stop flag (the stop belongs to the conversation, not to an id), and the
# main transcript's open work. The subagent bookkeeping stays with the old
# id: it names files under the old id's directory.
_MOVED = ("root_started", "root_start_ns", "content_stopped", "open_turns",
          "open_tools", "open_task_spans", "last_ns", "context",
          "title_custom", "title_ai", "root_name_sent")

HANDOFF_LOCK_TIMEOUT_S = 1.0


def _stop_heartbeat(session_id: str, home: str) -> None:
    """What hook.py does on SessionEnd: tell that id's pinger to stop."""
    try:
        with open(state_mod.session_file(session_id, home, ".heartbeat.stop"),
                  "w") as fh:
            fh.write("")
    except OSError:
        pass


def _take_over(st: dict, old_id: str, old_path: str, switch_ns: int,
               old: dict) -> None:
    conversation = old.get("conversation") or old_id
    st["conversation"] = conversation
    st["continued_from"] = old_id
    st["continued_from_path"] = old_path
    st["switch_ns"] = switch_ns
    st["copied_until"] = old.get("offset", 0)
    st["skipping_copied"] = True
    adopted = [a for a in (old.get("adopted") or []) if isinstance(a, dict)]
    st["adopted"] = adopted + [{"session_id": old_id, "transcript_path": old_path}]
    for key in _MOVED:
        if key in old:
            st[key] = old[key]
    # A root the old id had already closed is re-closed, later, by this id.
    st["finalized"] = False
    gen = old.get("open_gen")
    if isinstance(gen, dict):
        # Its byte offset points into the OLD file; never re-read with it.
        st["open_gen"] = dict(gen, offset=-1)
    old["open_turns"] = {}
    old["open_tools"] = {}
    old["open_task_spans"] = []
    old["open_gen"] = None
    old["finalized"] = True


def link(session_id: str, transcript_path: str, home: str,
         st: Optional[dict] = None, final: bool = False) -> Optional[str]:
    """Take the conversation over if this id continues another one.

    Returns the old id when this call made the switch, else None. `st` is
    this id's state when the caller already holds its lock (the exporter);
    without it the lock is taken here (hook.py, at SessionStart, which must
    stop the old id's pinger before it starts this one's). Looked up until
    found, or until `final` says the transcript has entries and none is
    coming: the copy is written when the new session starts.
    """
    if st is not None:
        if st.get("continuation_checked") or not transcript_path:
            return None
        return _link_into(st, session_id, home, final,
                          find_predecessor(transcript_path, session_id))
    # hook.py runs this for every SessionStart that has a key, in folders
    # that are off too. Read-only until there is a conversation to take
    # over: an untraced session must leave nothing on disk.
    if not transcript_path:
        return None
    found = find_predecessor(transcript_path, session_id)
    if found is None or not _was_traced(found[0], home):
        return None
    with state_mod.session_lock(session_id, home,
                                block_timeout=HANDOFF_LOCK_TIMEOUT_S) as got:
        if not got:
            return None
        st = state_mod.load(session_id, home)
        if st.get("continuation_checked"):
            return None
        old_id = _link_into(st, session_id, home, final, found)
        if old_id:
            state_mod.save(session_id, home, st)
        return old_id


def _was_traced(session_id: str, home: str) -> bool:
    """Whether the old id has state: a conversation never traced has
    nothing to take over, and looking must not create its files. An id
    read from a transcript is never trusted to name a file."""
    if not state_mod.is_valid_session_id(session_id):
        return False
    return os.path.exists(os.path.join(home, ".claude", "rius", "state",
                                       session_id + ".json"))


def _link_into(st: dict, session_id: str, home: str, final: bool,
               found) -> Optional[str]:
    if found is not None and not _was_traced(found[0], home):
        found = None
    if found is None:
        if final:
            st["continuation_checked"] = True
        return None
    old_id, old_path, switch_ns = found
    with state_mod.session_lock(old_id, home,
                                block_timeout=HANDOFF_LOCK_TIMEOUT_S) as got:
        if not got:
            return None                     # try again on the next event
        old = state_mod.load(old_id, home)
        if old.get("handed_off_to"):
            # Taken over already, and its open work moved with it: doing it
            # twice would move nothing and forget the root.
            st["continuation_checked"] = True
            return None
        _take_over(st, old_id, old_path, switch_ns, old)
        old["handed_off_to"] = session_id
        state_mod.save(old_id, home, old)
    st["continuation_checked"] = True
    _stop_heartbeat(old_id, home)
    return old_id
