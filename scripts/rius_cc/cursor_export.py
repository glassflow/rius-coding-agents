"""Cursor's half of the hook -> exporter hand-off.

The hook (cursor_hook.py) appends each event to the conversation's spool and
says when an export is due. The exporter rebuilds the whole tree from the
spool (cursor_spans.py) and sends only the spans that changed since the last
accepted export; `cursor_sent` in the session state is what it remembers.

A subagent's own events are spooled under its own id. Its trace, config and
state are its root conversation's, found through the `.parent` link written
when the subagent started. The link also names the Task call the subagent
answers, so one call is never given a second subagent.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

from . import agent, cursor_events, cursor_spans, platform_compat, state

SPOOL_DIRNAME = "spool"
PARENT_SUFFIX = cursor_events.PARENT_SUFFIX
TICKS_SUFFIX = ".ticks"
CLAIM_SUFFIX = ".claim"

# Exported at once: the session opening, a turn or subagent finishing, the
# session closing. Long turns also export every EXPORT_EVERY_N_TOOLS tools.
EXPORT_EVENTS = ("sessionStart", "stop", "subagentStop", "sessionEnd")
FINAL_EVENTS = ("stop", "sessionEnd")
TOOL_DONE_EVENTS = ("postToolUse", "postToolUseFailure")
EXPORT_EVERY_N_TOOLS = 20

# With capture on, a spool holds prompts and tool output, so it is deleted
# once its conversation has been idle this long and its trace is closed.
SPOOL_RETENTION_S = 7 * 24 * 3600
_SPOOL_FILE_SUFFIXES = (cursor_events.SPOOL_SUFFIX, PARENT_SUFFIX,
                        TICKS_SUFFIX, CLAIM_SUFFIX)
# A link is written to a temp file first; one left behind by a killed hook
# is no conversation's, so only its age says when it can go.
LINK_TEMP_TTL_S = 600

_PENDING, _FINISHED = "p:", "f:"


def spool_dir(home: str) -> str:
    return os.path.join(agent.active().rius_dir(home), SPOOL_DIRNAME)


def _sidecar(sdir: str, conversation_id: str, suffix: str) -> str:
    path = cursor_events.spool_path(sdir, conversation_id)
    return path[:-len(cursor_events.SPOOL_SUFFIX)] + suffix


def link_subagent(sdir: str, subagent_id: str, parent_id: str) -> None:
    """Record whose subagent this is. Replaced whole, so a reader never sees
    half a link."""
    os.makedirs(sdir, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".link-", suffix=".tmp", dir=sdir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(parent_id)
        platform_compat.replace_atomic(
            tmp, _sidecar(sdir, subagent_id, PARENT_SUFFIX))
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def root_conversation(sdir: str, conversation_id: str) -> str:
    current = conversation_id
    for _ in range(cursor_events.MAX_SUBAGENT_DEPTH + 1):
        parent = cursor_events.read_link(
            _sidecar(sdir, current, PARENT_SUFFIX))
        if not parent or parent == current:
            return current
        current = parent
    return current


# A subagent's first event follows its parent's Task call by seconds. That
# call is among the parent's last events, so only a spool's tail is read, and
# only the newest few spools are looked at.
TASK_LINK_WINDOW_NS = 120 * 10**9
TASK_LINK_TAIL_BYTES = 256 * 1024
TASK_LINK_CANDIDATES = 20


def _open_task_calls(events: List[Dict[str, Any]]) -> Dict[str, int]:
    """Task calls with no end yet, oldest first, each with the time it
    began. `cursor-agent -p` sends no postToolUse for a Task, so there only
    a sessionEnd ends one."""
    open_calls: Dict[str, int] = {}
    for event in events:
        kind = event.get("event")
        call = cursor_events.call_id(event.get("tool_use_id"))
        if kind == "sessionEnd":
            open_calls.clear()
        elif kind == "preToolUse" and event.get("tool_name") == "Task" and call:
            open_calls.setdefault(call, event["ts"])
        elif kind in TOOL_DONE_EVENTS:
            open_calls.pop(call, None)
    return open_calls


def _claim_path(sdir: str, parent: str, call: str, generation: int = 0) -> str:
    digest = hashlib.sha256(call.encode("utf-8")).hexdigest()[:16]
    again = ".%d" % generation if generation else ""
    return _sidecar(sdir, parent, "." + digest + again + CLAIM_SUFFIX)


# A claim file reads empty between its creation and its first write. One
# that stays empty past CLAIM_STALE_S was left by a hook killed in between.
CLAIM_READ_ATTEMPTS = 5
CLAIM_READ_PAUSE_S = 0.01
CLAIM_STALE_S = 5
CLAIM_GENERATIONS = 5


def _held_by(path: str, child: str) -> Optional[bool]:
    """Whether the claim at `path` is `child`'s; None if nobody holds it
    and nobody is about to: it is empty and stale."""
    for _ in range(CLAIM_READ_ATTEMPTS):
        try:
            with open(path, encoding="utf-8") as fh:
                holder = fh.read()
            age = time.time() - os.path.getmtime(path)
        except OSError:
            return False
        if holder:
            return holder == child
        if age > CLAIM_STALE_S:
            return None
        time.sleep(CLAIM_READ_PAUSE_S)
    return False


def claim_task_call(sdir: str, parent: str, call: str, child: str) -> bool:
    """Give `child` the Task call `parent` made, if no other subagent has it.

    One subagent per call, decided by a file only one hook can create (hooks
    run as separate processes, at the same moment when subagents start
    together). The holder asking again, as when two of its first hooks race,
    gets the call it already has. A stale empty claim is passed over for the
    next generation of the file, never deleted: two hooks that both find it
    stale would otherwise both delete and re-create it.
    """
    os.makedirs(sdir, mode=0o700, exist_ok=True)
    for generation in range(CLAIM_GENERATIONS):
        path = _claim_path(sdir, parent, call, generation)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            held = _held_by(path, child)
            if held is None:
                continue
            return held
        try:
            os.write(fd, child.encode("utf-8"))
        finally:
            os.close(fd)
        return True
    return False


def _claim_open_call(sdir: str, parent: str, events: List[Dict[str, Any]],
                     child: str, since_ns: int) -> str:
    """The oldest of the Task calls still open in `events`, begun since
    `since_ns`, that `child` can have; "" if there is none."""
    for call, began in _open_task_calls(events).items():
        if began >= since_ns and claim_task_call(sdir, parent, call, child):
            return call
    return ""


def _tail(sdir: str, conversation_id: str) -> List[Dict[str, Any]]:
    return cursor_events.read_spool(
        cursor_events.spool_path(sdir, conversation_id), TASK_LINK_TAIL_BYTES)


def claim_oldest_task_call(sdir: str, parent: str, child: str) -> str:
    """Claim for `child`, a subagent the env named `parent` for, the oldest
    Task call of `parent` that no subagent has. "" if there is none."""
    try:
        return _claim_open_call(sdir, parent, _tail(sdir, parent), child, 0)
    except Exception:  # a hook must never fail the agent
        return ""


def _recent_conversations(sdir: str, since_s: float) -> List[str]:
    """Conversations whose spool changed since `since_s`, newest first."""
    try:
        names = os.listdir(sdir)
    except OSError:
        return []
    recent = []
    for name in names:
        cid = name[:-len(cursor_events.SPOOL_SUFFIX)]
        if (not name.endswith(cursor_events.SPOOL_SUFFIX)
                or not state.is_valid_session_id(cid)):
            continue
        try:
            changed = os.path.getmtime(os.path.join(sdir, name))
        except OSError:
            continue
        if changed >= since_s:
            recent.append((changed, cid))
    return [cid for _, cid in sorted(recent, reverse=True)]


def claim_awaiting_task_call(sdir: str, workspace: str, child: str,
                             now_ns: Optional[int] = None
                             ) -> Optional[Tuple[str, str]]:
    """(conversation, Task call) that `child`, a subagent whose events name
    no parent, now holds: a call begun in the last TASK_LINK_WINDOW_NS by a
    conversation working in `workspace`, that no subagent had.

    Reads spools, which the hook otherwise never does, so it is for a
    conversation that has no spool yet and no env naming its parent.
    """
    if not workspace:
        return None
    try:
        return _claim_awaiting(sdir, workspace, child, now_ns)
    except Exception:  # a hook must never fail the agent
        return None


def _claim_awaiting(sdir: str, workspace: str, child: str,
                    now_ns: Optional[int]) -> Optional[Tuple[str, str]]:
    now = time.time_ns() if now_ns is None else now_ns
    since_ns = now - TASK_LINK_WINDOW_NS
    recent = _recent_conversations(sdir, since_ns / 1e9)
    for cid in recent[:TASK_LINK_CANDIDATES]:
        events = _tail(sdir, cid)
        if not any(e.get("workspace") == workspace for e in events):
            continue
        call = _claim_open_call(sdir, cid, events, child, since_ns)
        if call:
            return cid, call
    return None


def tick(sdir: str, conversation_id: str) -> int:
    """Count one finished tool; returns the count so far.

    One byte appended per tool, so the count is the file's size and no
    hook ever has to read-modify-write it.
    """
    path = _sidecar(sdir, conversation_id, TICKS_SUFFIX)
    os.makedirs(sdir, mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, b".")
    finally:
        os.close(fd)
    return os.path.getsize(path)


def _stem(sdir: str, conversation_id: str) -> str:
    path = cursor_events.spool_path(sdir, conversation_id)
    return os.path.basename(path)[:-len(cursor_events.SPOOL_SUFFIX)]


def _split_spool_name(name: str) -> Optional[str]:
    for suffix in _SPOOL_FILE_SUFFIXES:
        if name.endswith(suffix):
            stem = name[:-len(suffix)]
            # <conversation>.<call digest>[.<generation>].claim
            return stem.partition(".")[0] if suffix == CLAIM_SUFFIX else stem
    return None


def _is_link_temp(name: str) -> bool:
    return name.startswith(".link-") and name.endswith(".tmp")


def _remove_if_older(path: str, cutoff_s: float) -> int:
    try:
        if os.path.getmtime(path) < cutoff_s:
            os.remove(path)
            return 1
    except OSError:
        pass
    return 0


def prune(sdir: str, open_ids: Iterable[str],
          now_s: Optional[float] = None) -> int:
    """Delete the spool files of conversations idle longer than
    SPOOL_RETENTION_S, except those whose trace is still open (`open_ids`):
    the stale sweep still needs their events to close it. Link temp files
    older than LINK_TEMP_TTL_S go too. Returns how many files went."""
    now = time.time() if now_s is None else now_s
    keep = {_stem(sdir, cid) for cid in open_ids}
    try:
        names = os.listdir(sdir)
    except OSError:
        return 0
    removed = 0
    for name in names:
        path = os.path.join(sdir, name)
        if _is_link_temp(name):
            removed += _remove_if_older(path, now - LINK_TEMP_TTL_S)
            continue
        stem = _split_spool_name(name)
        if stem is None or _stem(sdir, root_conversation(sdir, stem)) in keep:
            continue
        removed += _remove_if_older(path, now - SPOOL_RETENTION_S)
    return removed


def export_due(event: str, tools_done: int = 0) -> bool:
    if event in EXPORT_EVENTS:
        return True
    return (event in TOOL_DONE_EVENTS
            and tools_done > 0 and tools_done % EXPORT_EVERY_N_TOOLS == 0)


def read_events(home: str, conversation_id: str) -> List[Dict[str, Any]]:
    return cursor_events.read_conversation(spool_dir(home), conversation_id)


def _digest(span) -> str:
    body = json.dumps([span.parent_span_id, span.name, span.start_ns,
                       span.end_ns, span.status_code, span.status_message,
                       span.pending, span.attributes, span.events],
                      sort_keys=True, default=str)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


def _unsent(spans, sent: Dict[str, str], closing_only: bool) -> list:
    """Spans whose current form has not been accepted yet.

    `closing_only` sends nothing that already went out finished: a session
    closed with capture off must not overwrite spans that carried content
    the user allowed at the time.
    """
    out = []
    for span in spans:
        mark = (_PENDING if span.pending else _FINISHED) + _digest(span)
        previous = sent.get(span.span_id, "")
        if previous == mark:
            continue
        if closing_only and previous.startswith(_FINISHED):
            continue
        out.append(span)
    return out


def _remember(sent: Dict[str, str], spans) -> None:
    for span in spans:
        sent[span.span_id] = ((_PENDING if span.pending else _FINISHED)
                              + _digest(span))


def _note_root(st: dict, root, events) -> None:
    """What the open-trace marker and the stale sweep go by."""
    st["root_started"] = True
    st["root_start_ns"] = root.start_ns
    st["last_ns"] = events[-1]["ts"]
    st["finalized"] = not root.pending


def spans_to_send(st: dict, events: List[Dict[str, Any]], conversation_id: str,
                  capture_content: bool, max_attr_bytes: int,
                  final_ns: Optional[int] = None,
                  closing_only: bool = False) -> list:
    """The spans to export now, recording them in `st` as if accepted.

    The caller saves `st` only once the receiver accepts them; on a retryable
    failure it reloads the state from disk, so they are sent again.
    """
    ctx = cursor_spans.Ctx(conversation_id, capture_content, max_attr_bytes)
    spans = cursor_spans.build(events, ctx, final_ns)
    if not spans:
        return []
    sent = dict(st.get("cursor_sent") or {})
    out = _unsent(spans, sent, closing_only)
    _remember(sent, out)
    st["cursor_sent"] = sent
    _note_root(st, spans[0], events)
    return out
