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
from typing import Any, Dict, List, Optional

from . import agent, cursor_events, cursor_spans

SPOOL_DIRNAME = "spool"
PARENT_SUFFIX = ".parent"
TICKS_SUFFIX = ".ticks"

# Exported at once: the session opening, a turn or subagent finishing, the
# session closing. Long turns also export every EXPORT_EVERY_N_TOOLS tools.
EXPORT_EVENTS = ("sessionStart", "stop", "subagentStop", "sessionEnd")
FINAL_EVENTS = ("stop", "sessionEnd")
TOOL_DONE_EVENTS = ("postToolUse", "postToolUseFailure")
EXPORT_EVERY_N_TOOLS = 20

_PENDING, _FINISHED = "p:", "f:"


def spool_dir(home: str) -> str:
    return os.path.join(agent.active().rius_dir(home), SPOOL_DIRNAME)


def _sidecar(sdir: str, conversation_id: str, suffix: str) -> str:
    path = cursor_events.spool_path(sdir, conversation_id)
    return path[:-len(cursor_events.SPOOL_SUFFIX)] + suffix


def link_subagent(sdir: str, subagent_id: str, parent_id: str) -> None:
    os.makedirs(sdir, mode=0o700, exist_ok=True)
    with open(_sidecar(sdir, subagent_id, PARENT_SUFFIX), "w") as fh:
        fh.write(parent_id)


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
