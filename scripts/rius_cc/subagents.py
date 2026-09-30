"""Subagent transcripts: the work that is NOT in the main transcript.

Claude Code does not inline a subagent's entries into the session transcript
-- the main file contains zero `isSidechain` entries. Each subagent writes its
own file:

    ~/.claude/projects/<proj>/<session-id>/subagents/agent-<agentid>.jsonl
    ~/.claude/projects/<proj>/<session-id>/subagents/agent-<agentid>.meta.json

The sibling `.meta.json` carries `toolUseId`, the exact id of the `Agent` (or
`Task`) tool_use that spawned that subagent, so the link between a tool span
and a subagent transcript is an EXACT match, never a guess. There is no mtime
fallback and there must not be one: guessing which subagent a tool call
spawned files one agent's tokens under another agent's name, which is worse
than the gap it would paper over. No match -> the tool span stands alone.

Why this file exists at all: in the acceptance session, 58% of the tokens and
71% of the model calls happened inside subagents. Stopping at the tool span
reported that session's cost at less than half its true value.

Zero third-party dependencies; standard library only.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, Tuple

from . import spans, transcript

META_SUFFIX = ".meta.json"

# A subagent can spawn a subagent (meta.json carries `spawnDepth`). The cap is
# a loop-stopper, not a policy: a cycle in the links -- however it arose --
# must not turn one hook event into an unbounded walk of the filesystem.
MAX_DEPTH = 5


def dir_for(transcript_path: str, session_id: str) -> str:
    """The subagents directory for a session's transcript path."""
    if not transcript_path:
        return ""
    parent = os.path.dirname(transcript_path)
    stem = os.path.basename(transcript_path)
    if stem.endswith(".jsonl"):
        stem = stem[:-len(".jsonl")]
    path = os.path.join(parent, stem, "subagents")
    if not os.path.isdir(path) and session_id and session_id != stem:
        alt = os.path.join(parent, session_id, "subagents")
        if os.path.isdir(alt):
            return alt
    return path


def index_by_tool_use(subdir: str) -> Dict[str, Tuple[str, dict, str]]:
    """{toolUseId: (agent_id, meta, jsonl_path)} for one subagents directory.

    An unreadable or malformed meta.json is skipped, not raised on: it is
    written by another process while we read it, and a half-written one must
    cost at most that one subagent.
    """
    out: Dict[str, Tuple[str, dict, str]] = {}
    try:
        names = sorted(os.listdir(subdir))
    except OSError:
        return out
    for name in names:
        if not name.endswith(META_SUFFIX):
            continue
        try:
            with open(os.path.join(subdir, name)) as fh:
                meta = json.load(fh)
        except (OSError, ValueError):
            continue
        if not isinstance(meta, dict):
            continue
        tool_use_id = meta.get("toolUseId")
        if not tool_use_id or not isinstance(tool_use_id, str):
            continue
        agent_id = name[:-len(META_SUFFIX)]
        out[tool_use_id] = (agent_id, meta,
                            os.path.join(subdir, agent_id + ".jsonl"))
    return out


def _ensure_keys(state: dict) -> None:
    for key in ("sub_links", "sub_offsets", "sub_scopes"):
        if not isinstance(state.get(key), dict):
            state[key] = {}


def _scope_for(state: dict, agent_id: str) -> dict:
    scope = state["sub_scopes"].get(agent_id)
    if not isinstance(scope, dict):
        scope = spans.new_scope()
        state["sub_scopes"][agent_id] = scope
    else:
        for key, value in spans.new_scope().items():
            if key not in scope:
                scope[key] = value
    return scope


# The brief is the first user entry of the subagent's file; the lines after
# it are attachments and system reminders. Bounded so a malformed file costs
# a few reads, not a walk of a 300 KB transcript.
PROMPT_SCAN_LINES = 20


def first_prompt(path: str) -> str:
    """The subagent's brief, read back from its file on demand.

    Deliberately NOT kept in state: state.save writes that dict to
    ~/.claude/rius/state/<sid>.json in plaintext and on every hook event, so
    caching one brief per subagent would both persist content the capture
    gate is supposed to control and rewrite it on every event for the rest of
    the session.
    """
    try:
        with open(path, "rb") as fh:
            for _ in range(PROMPT_SCAN_LINES):
                raw = fh.readline()
                if not raw:
                    break
                entry = transcript.parse_line(raw.decode("utf-8", "replace"))
                if entry is None or entry.kind != "user":
                    continue
                if entry.tool_results():
                    continue
                text = entry.text()
                if text:
                    return text
    except OSError:
        return ""
    return ""


def _agent_span(ctx, trace_id, link, agent_id, meta, path, end_ns, pending,
                start_ns=None):
    # A pending span carries no content, so the file is not even opened for
    # one -- and with capture off it is never opened for this at all.
    prompt = ""
    if not pending and ctx.capture_content:
        prompt = first_prompt(path)
    return spans.subagent_span(
        ctx, trace_id,
        span_id=spans.span_id_for("subagent:" + agent_id),
        parent_span_id=link["span_id"], meta=meta, agent_id=agent_id,
        depth=link.get("depth") or 1,
        start_ns=start_ns or link.get("start_ns") or 0, end_ns=end_ns,
        prompt=prompt, pending=pending,
    )


def _expand_one(state: dict, ctx, trace_id: str, link: dict, agent_id: str,
                meta: dict, path: str) -> List[Any]:
    out: List[Any] = []
    scope = _scope_for(state, agent_id)
    agent_span_id = spans.span_id_for("subagent:" + agent_id)
    offset = state["sub_offsets"].get(agent_id) or 0
    entries, new_offset = transcript.read_from(path, offset)
    state["sub_offsets"][agent_id] = new_offset

    if not scope.get("started"):
        if not entries:
            # meta.json can be written before the subagent's first line.
            # Nothing has run yet; open the span once something has.
            return out
        scope["started"] = True
        # The subagent starts with its own first line. The Agent tool_use is
        # stamped when that block finished streaming, and a parallel batch of
        # Agent calls only runs once the whole response has: 24 s earlier,
        # in one real session. Fixed here, once: the backend collapses the
        # pending and final copies only if the start matches.
        start_ns = max(link.get("start_ns") or 0, entries[0].timestamp_ns)
        scope["start_ns"] = start_ns
        # Pending first, exactly like the session root and every tool span:
        # a subagent that is still running should draw as in-progress rather
        # than appear only once it finishes.
        out.append(_agent_span(ctx, trace_id, link, agent_id, meta, path,
                               end_ns=start_ns, pending=True,
                               start_ns=start_ns))

    out += spans.emit_entries(
        entries, scope, ctx, trace_id, agent_span_id, state["sub_links"],
        depth=link.get("depth") or 1,
        key_prefix="agent:" + agent_id + ":",
        # A subagent's generations hang off its own AGENT span. Its entries
        # are all sidechain entries, so the main transcript's inline-sidechain
        # re-parenting would put them under whatever tool ran last.
        make_turns=False, inline_sidechains=False, source_path=path,
    )

    # The tool_result is when the Agent CALL returned. For a foreground
    # subagent that is also when it finished; a background one (Claude Code
    # 2.1.x's default) returns a launch acknowledgement at once and keeps
    # writing its file for minutes. So the subagent ends at whichever is
    # later, and its span is re-sent, same id and start, as its file grows.
    end_ns = link.get("end_ns")
    if end_ns is not None:
        agent_end = max(end_ns, scope.get("last_ns") or 0)
        if not link.get("closed") or agent_end > (link.get("emitted_end_ns") or 0):
            link["closed"] = True
            link["emitted_end_ns"] = agent_end
            out.append(_agent_span(ctx, trace_id, link, agent_id, meta, path,
                                   end_ns=agent_end, pending=False,
                                   start_ns=scope.get("start_ns")))
    return out


def _has_news(state: dict, subdir: str, agent_id: str) -> bool:
    """Whether a subagent's file has grown past what was already read."""
    try:
        size = os.path.getsize(os.path.join(subdir, agent_id + ".jsonl"))
    except OSError:
        return False
    return size > (state["sub_offsets"].get(agent_id) or 0)


def expand(state: dict, ctx, subdir: str) -> List[Any]:
    """Spans for every subagent spawned by a tool_use seen so far.

    Streams: each subagent file has its own byte offset in
    state["sub_offsets"], so a hook event emits only what is new, the same
    way the main transcript does. Recurses: a subagent's own `Agent` tool_use
    registers a link while its entries are being read, and the loop picks it
    up on the next pass.
    """
    _ensure_keys(state)
    out: List[Any] = []
    if not subdir or not state["sub_links"] or not os.path.isdir(subdir):
        return out

    trace_id = spans.trace_id_for(ctx.conversation_id)
    index: Optional[Dict[str, Tuple[str, dict, str]]] = None
    seen = set()

    while True:
        todo = [tid for tid in list(state["sub_links"]) if tid not in seen]
        if not todo:
            break
        for tool_use_id in todo:
            seen.add(tool_use_id)
            link = state["sub_links"][tool_use_id]
            # A closed link is NOT finished with: a background subagent keeps
            # writing after its tool call returned. Only a file that has not
            # grown is skipped, and that costs one stat, not an index.
            if (link.get("closed") and link.get("agent_id")
                    and not _has_news(state, subdir, link["agent_id"])):
                continue
            if (link.get("depth") or 1) > MAX_DEPTH:
                continue
            if index is None:
                index = index_by_tool_use(subdir)
            found = index.get(tool_use_id)
            if found is None:
                # No meta.json names this tool_use: the tool span stands
                # alone. Nothing is guessed from mtime or ordering.
                continue
            agent_id, meta, path = found
            link["agent_id"] = agent_id
            out += _expand_one(state, ctx, trace_id, link, agent_id, meta, path)
    return out


def finalize(state: dict, ctx, subdir: str, now_ns: int) -> List[Any]:
    """Close subagent AGENT spans still open when the session ends.

    A subagent whose tool_result never arrived (the session died mid-run)
    would otherwise leave a pending span forever, the same problem the
    session root has and for the same reason.
    """
    _ensure_keys(state)
    out: List[Any] = []
    if not subdir or not state["sub_links"] or not os.path.isdir(subdir):
        return out
    trace_id = spans.trace_id_for(ctx.conversation_id)
    index = index_by_tool_use(subdir)
    for tool_use_id, link in list(state["sub_links"].items()):
        if link.get("closed"):
            continue
        found = index.get(tool_use_id)
        if found is None:
            continue
        agent_id, meta, path = found
        scope = _scope_for(state, agent_id)
        if not scope.get("started"):
            continue
        link["closed"] = True
        out.append(_agent_span(ctx, trace_id, link, agent_id, meta, path,
                               end_ns=now_ns, pending=False,
                               start_ns=scope.get("start_ns")))
    return out
