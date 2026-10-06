"""Cursor's half of the hook -> exporter hand-off.

The hook (cursor_hook.py) appends each event to the conversation's spool and
says when an export is due. The exporter rebuilds the whole tree from the
spool (cursor_spans.py) and sends only the spans that changed since the last
accepted export; `cursor_sent` in the session state is what it remembers.

A subagent's own events are spooled under its own id. Its trace, config and
state are its root conversation's, found through the `.parent` link written
when the subagent started.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Any, Dict, Iterable, List, Optional

from . import agent, cursor_events, cursor_spans, state

SPOOL_DIRNAME = "spool"
PARENT_SUFFIX = cursor_events.PARENT_SUFFIX
TICKS_SUFFIX = ".ticks"

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
                        TICKS_SUFFIX)

_PENDING, _FINISHED = "p:", "f:"


def spool_dir(home: str) -> str:
    return os.path.join(agent.active().rius_dir(home), SPOOL_DIRNAME)


def _sidecar(sdir: str, conversation_id: str, suffix: str) -> str:
    path = cursor_events.spool_path(sdir, conversation_id)
    return path[:-len(cursor_events.SPOOL_SUFFIX)] + suffix


def link_subagent(sdir: str, subagent_id: str, parent_id: str) -> None:
    os.makedirs(sdir, mode=0o700, exist_ok=True)
    fd = os.open(_sidecar(sdir, subagent_id, PARENT_SUFFIX),
                 os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, parent_id.encode("utf-8"))
    finally:
        os.close(fd)


def root_conversation(sdir: str, conversation_id: str) -> str:
    current = conversation_id
    for _ in range(cursor_events.MAX_SUBAGENT_DEPTH + 1):
        try:
            with open(_sidecar(sdir, current, PARENT_SUFFIX)) as fh:
                parent = fh.read().strip()
        except OSError:
            return current
        if not parent or parent == current:
            return current
        current = parent
    return current


# A subagent's first event follows its parent's Task call by seconds. The
# call is among the parent's last events, so only the spool's tail is read.
TASK_LINK_WINDOW_S = 600
TASK_LINK_TAIL_BYTES = 256 * 1024


def _inside_task_call(events: List[Dict[str, Any]]) -> bool:
    """True while a Task call has not ended. `cursor-agent -p` sends no
    postToolUse for a Task, so there only a sessionEnd ends one."""
    open_calls = set()
    for event in events:
        kind = event.get("event")
        if kind == "sessionEnd":
            open_calls.clear()
        elif kind == "preToolUse" and event.get("tool_name") == "Task":
            open_calls.add(event.get("tool_use_id"))
        elif kind in TOOL_DONE_EVENTS:
            open_calls.discard(event.get("tool_use_id"))
    return bool(open_calls)


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


def conversation_in_task_call(sdir: str, workspace: str,
                              now_s: Optional[float] = None) -> str:
    """The conversation working in `workspace` that is waiting on a Task
    call, or "": the parent of a subagent whose events name no one.

    Reads spools, which the hook otherwise never does, so it is for a
    conversation that has no spool yet and no env naming its parent.
    """
    if not workspace:
        return ""
    since = (time.time() if now_s is None else now_s) - TASK_LINK_WINDOW_S
    for cid in _recent_conversations(sdir, since):
        events = cursor_events.read_spool(
            cursor_events.spool_path(sdir, cid), TASK_LINK_TAIL_BYTES)
        if (any(e.get("cwd") == workspace for e in events)
                and _inside_task_call(events)):
            return cid
    return ""


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
            return name[:-len(suffix)]
    return None


def prune(sdir: str, open_ids: Iterable[str],
          now_s: Optional[float] = None) -> int:
    """Delete the spool files of conversations idle longer than
    SPOOL_RETENTION_S, except those whose trace is still open (`open_ids`):
    the stale sweep still needs their events to close it. Returns how many
    files went."""
    cutoff = (time.time() if now_s is None else now_s) - SPOOL_RETENTION_S
    keep = {_stem(sdir, cid) for cid in open_ids}
    try:
        names = os.listdir(sdir)
    except OSError:
        return 0
    removed = 0
    for name in names:
        stem = _split_spool_name(name)
        if stem is None or _stem(sdir, root_conversation(sdir, stem)) in keep:
            continue
        path = os.path.join(sdir, name)
        try:
            if os.path.getmtime(path) < cutoff:
                os.remove(path)
                removed += 1
        except OSError:
            continue
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
