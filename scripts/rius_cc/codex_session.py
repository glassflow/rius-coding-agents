"""One Codex session's hook events -> spans, its subagents included.

Glue between the hook payloads, the rollout reader and the span builder; the
exporter does the locking, shipping and state persistence around it.

A subagent runs in its own rollout file but fires its hooks under the
parent's session id, with `agent_id` set and `transcript_path` naming ITS
rollout. So the parent's rollout path is remembered from the parent's own
events, and each subagent keeps its own offset and builder state.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Mapping, Optional, Tuple

from . import codex_rollout, codex_spans, state
from .spans import Ctx, Span

SUBAGENT_STOP = "SubagentStop"


def load(st: dict) -> dict:
    """The session state with the Codex builder's keys filled in."""
    for key, value in codex_spans.new_state().items():
        st.setdefault(key, value)
    st.setdefault("codex_subs", {})
    return st


def note_payload(st: dict, event: str,
                 payload: Mapping[str, Any]) -> Optional[str]:
    """Remember what this event says about rollout files. Returns the
    parent's rollout path, or None when it is not known yet."""
    path = payload.get("transcript_path") or ""
    agent_id = payload.get("agent_id")
    if agent_id and not state.is_valid_session_id(agent_id):
        return st.get("transcript_path") or None
    sub_path = ""
    if event == SUBAGENT_STOP:
        # transcript_path names the rollout of the agent that SPAWNED this
        # one, a subagent's when nested: not necessarily the session's.
        sub_path = payload.get("agent_transcript_path") or ""
    elif agent_id:
        sub_path = path
    if path and (not agent_id or (event == SUBAGENT_STOP
                                  and _may_be_parents(st, path))):
        _set_parent_path(st, path)
    if agent_id:
        _remember(st, agent_id, sub_path, payload.get("agent_type") or "")
    return st.get("transcript_path") or None


def _may_be_parents(st: dict, path: str) -> bool:
    """A SubagentStop's transcript_path, as a last resort before any of the
    parent's own events named its rollout: unless a subagent's it is."""
    return not st.get("transcript_path") and all(
        sub.get("path") != path for sub in st["codex_subs"].values())


def _set_parent_path(st: dict, path: str) -> None:
    """An offset only means something in the file it was read from."""
    if st.get("transcript_path") and st["transcript_path"] != path:
        st["offset"] = 0
    st["transcript_path"] = path


def _remember(st: dict, agent_id: str, path: str, agent_type: str) -> None:
    sub = st["codex_subs"].setdefault(
        agent_id, {"path": "", "agent_type": "", "offset": 0, "state": None})
    if path:
        sub["path"] = path
    if agent_type:
        sub["agent_type"] = agent_type


def build(st: dict, ctx: Ctx, rollout_path: Optional[str]) -> Tuple[List[Span], int]:
    """Spans for everything written since the last call. Returns
    (spans, the parent rollout's new offset)."""
    offset = st.get("offset", 0)
    out: List[Span] = []
    if rollout_path:
        records, offset = codex_rollout.read_from(rollout_path, offset)
        out += codex_spans.build(records, st, ctx)
        _seen(st, records)
    out += _build_subagents(st, ctx)
    return out, offset


def _seen(st: dict, records: list) -> None:
    if records:
        st["last_ns"] = max(st.get("last_ns") or 0, records[-1].timestamp_ns)


def _spawned(st: dict) -> Dict[str, str]:
    """Every subagent spawned so far, by any agent of the session."""
    spawned = dict(st.get("spawned") or {})
    for sub in st["codex_subs"].values():
        spawned.update((sub.get("state") or {}).get("spawned") or {})
    return spawned


def _build_subagents(st: dict, ctx: Ctx) -> List[Span]:
    out: List[Span] = []
    for agent_id, parent_span_id in _spawned(st).items():
        if not state.is_valid_session_id(agent_id):
            continue
        sub = st["codex_subs"].get(agent_id)
        if not sub or not sub.get("path"):
            path = _beside_parent(st, agent_id)
            if not path:
                continue
            _remember(st, agent_id, path, "")
            sub = st["codex_subs"][agent_id]
        if sub.get("state") is None:
            sub["state"] = codex_spans.new_subagent_state(
                agent_id, sub.get("agent_type") or "", parent_span_id)
        records, sub["offset"] = codex_rollout.read_from(
            sub["path"], sub.get("offset", 0))
        out += codex_spans.build(records, sub["state"], ctx)
        _seen(st, records)
    return out


def _beside_parent(st: dict, agent_id: str) -> str:
    """A subagent whose hooks have not named its rollout yet: Codex writes
    it next to the parent's, ending in the agent's thread id."""
    parent = st.get("transcript_path") or ""
    folder = os.path.dirname(parent)
    suffix = "-%s.jsonl" % agent_id
    try:
        names = os.listdir(folder) if folder else []
    except OSError:
        return ""
    for name in names:
        if name.startswith("rollout-") and name.endswith(suffix):
            return os.path.join(folder, name)
    return ""


def finalize(st: dict, ctx: Ctx, end_ns: int) -> List[Span]:
    """Close every open span of the session and of its subagents."""
    out: List[Span] = []
    for sub in st["codex_subs"].values():
        if sub.get("state") is not None:
            out += codex_spans.finalize_session(sub["state"], ctx, end_ns)
    return out + codex_spans.finalize_session(st, ctx, end_ns)


def resource_attributes(service_name: str, instance_id: str,
                        st: dict) -> Dict[str, str]:
    attrs = {"service.name": service_name, "service.instance.id": instance_id}
    attrs.update(codex_spans.resource_attributes(st))
    return attrs
