"""Codex rollout records -> the span tree spans.py builds for Claude Code.

The same Span objects, ids, pending convention and capture gate, so otlp.py
encodes them unchanged. The tree:

    AGENT  "codex session"            session:<thread id>
      CHAIN  turn                     one per Codex turn id
        LLM  <model>                  one per token_count (one model call)
          TOOL  <tool>                function_call / custom_tool_call, by call_id
            AGENT  <agent type>       a subagent, built from its own rollout
              CHAIN  turn  ...        with a state of its own (new_subagent_state)

Token counts are OpenAI's: `input_tokens` already includes the cached ones,
which is what argus-core pricing expects, so it is sent as given with
`cached_input_tokens` as the cache-read subset. Reasoning tokens are part of
`output_tokens` and are only reported as their own split.

Everything in `state` is plain JSON, persisted between hook invocations.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from . import codex_rollout as cr
from .spans import (ERROR_MESSAGE_MAX_BYTES, TOOL_ERROR_WITHHELD, Ctx, Span,
                    _base_attrs, _content_attr, span_id_for, trace_id_for,
                    truncate)

PROVIDER_NAME = "openai"
DEFAULT_ROOT_NAME = "codex session"

# How Codex's exec_command reports a command's end, and where its output starts.
_EXIT_CODE = re.compile(r"^Process exited with code (-?\d+)\s*$", re.MULTILINE)
_OUTPUT_MARKER = "\nOutput:\n"

SPAWN_TOOL = "multi_agent_v1__spawn_agent"


def new_state() -> dict:
    return {"root_started": False, "root_start_ns": 0, "finalized": False,
            "session": {}, "model": "", "turn": None, "open_tools": {},
            "spawned": {}}


def new_subagent_state(agent_id: str, agent_type: str,
                       parent_span_id: str) -> dict:
    """State for one subagent's rollout: its root hangs under the
    spawn_agent call that started it, in the parent's trace."""
    state = new_state()
    state.update({"root_id": span_id_for("subagent:" + agent_id),
                  "root_parent": parent_span_id,
                  "root_name": agent_type or "subagent",
                  "root_attrs": {"gen_ai.agent.name": agent_type,
                                 "codex.subagent.id": agent_id}})
    return state


def _attrs(ctx: Ctx, kind_oi: str) -> Dict[str, Any]:
    attrs = _base_attrs(ctx, kind_oi)
    if "gen_ai.provider.name" in attrs:
        attrs["gen_ai.provider.name"] = PROVIDER_NAME
    return attrs


def _root_span_id(state: dict, ctx: Ctx) -> str:
    return (state.get("root_id")
            or span_id_for("session:" + ctx.conversation_id))


def _llm_span_id(turn: dict) -> str:
    return span_id_for("llm:%s:%d" % (turn["turn_id"], turn["llm_index"]))


def _pending(ctx: Ctx, span_id: str, parent: Optional[str], name: str,
             kind_oi: str, start_ns: int, extra: Dict[str, Any]) -> Span:
    attrs = _attrs(ctx, kind_oi)
    attrs.update(extra)
    attrs["glassflow.span.pending"] = True
    return Span(trace_id=trace_id_for(ctx.conversation_id), span_id=span_id,
                parent_span_id=parent, name=name, kind_oi=kind_oi,
                start_ns=start_ns, end_ns=start_ns, attributes=attrs,
                status_code="UNSET", status_message="", pending=True)


def _finished(ctx: Ctx, span_id: str, parent: Optional[str], name: str,
              kind_oi: str, start_ns: int, end_ns: int, attrs: Dict[str, Any],
              status_code: str = "OK", status_message: str = "",
              events: Optional[list] = None) -> Span:
    return Span(trace_id=trace_id_for(ctx.conversation_id), span_id=span_id,
                parent_span_id=parent, name=name, kind_oi=kind_oi,
                start_ns=start_ns, end_ns=end_ns, attributes=attrs,
                status_code=status_code, status_message=status_message,
                pending=False, events=events)


def _kept(ctx: Ctx, text: str) -> str:
    """Content that may wait in the state file: none with capture off."""
    return text if ctx.capture_content else ""


def _on_session(rec, state, ctx, out):
    state["session"] = {k: rec.get(k, "") for k in
                        ("cli_version", "cwd", "originator", "git_branch")}


def _on_turn_context(rec, state, ctx, out):
    if rec.get("model"):
        state["model"] = rec.get("model")


def _on_turn_start(rec, state, ctx, out):
    if state["turn"] is not None:
        out += _close_turn(state, ctx, rec.timestamp_ns, None)
    turn_id = rec.get("turn_id")
    state["turn"] = {"turn_id": turn_id, "span_id": span_id_for("turn:" + turn_id),
                     "start_ns": rec.timestamp_ns, "text": "", "reply": "",
                     "llm_index": 0, "llm_start_ns": rec.timestamp_ns}
    out.append(_pending(ctx, state["turn"]["span_id"],
                        _root_span_id(state, ctx), "turn", "CHAIN",
                        rec.timestamp_ns, {}))


def _on_user_message(rec, state, ctx, out):
    turn = state["turn"]
    turn["text"] = _kept(ctx, rec.get("text"))
    turn["llm_start_ns"] = rec.timestamp_ns


def _on_agent_message(rec, state, ctx, out):
    turn = state["turn"]
    turn["reply"] += _kept(ctx, rec.get("text"))


def _usage_attrs(ctx: Ctx, model: str, rec) -> Dict[str, Any]:
    attrs = _attrs(ctx, "LLM")
    if model:
        attrs["gen_ai.request.model"] = model
        attrs["gen_ai.response.model"] = model
    attrs["gen_ai.usage.input_tokens"] = rec.get("input_tokens")
    attrs["gen_ai.usage.output_tokens"] = rec.get("output_tokens")
    attrs["gen_ai.usage.cache_read.input_tokens"] = rec.get("cached_input_tokens")
    attrs["gen_ai.usage.reasoning.output_tokens"] = rec.get("reasoning_output_tokens")
    return attrs


def _on_usage(rec, state, ctx, out):
    turn = state["turn"]
    attrs = _usage_attrs(ctx, state["model"], rec)
    _content_attr(ctx, attrs, "output.value", turn["reply"])
    out.append(_finished(ctx, _llm_span_id(turn), turn["span_id"],
                         state["model"] or "model call", "LLM",
                         turn["llm_start_ns"], rec.timestamp_ns, attrs))
    turn["llm_index"] += 1
    turn["llm_start_ns"] = rec.timestamp_ns
    turn["reply"] = ""


def _on_tool_call(rec, state, ctx, out):
    call_id = rec.get("call_id")
    tool = {"span_id": span_id_for(call_id), "parent_span_id": _llm_span_id(state["turn"]),
            "start_ns": rec.timestamp_ns, "tool_name": rec.get("name"),
            "input_json": _kept(ctx, rec.get("arguments")), "mcp_error": None}
    state["open_tools"][call_id] = tool
    out.append(_pending(ctx, tool["span_id"], tool["parent_span_id"],
                        tool["tool_name"], "TOOL", rec.timestamp_ns,
                        {"gen_ai.tool.name": tool["tool_name"]}))


def _on_mcp_result(rec, state, ctx, out):
    tool = state["open_tools"].get(rec.get("call_id"))
    if tool is not None and rec.get("is_error"):
        tool["mcp_error"] = _kept(ctx, rec.get("error"))


def _command_output(output: str) -> str:
    head, marker, body = output.partition(_OUTPUT_MARKER)
    return body if marker else output


def _mcp_text(body: str) -> str:
    """The text parts of an MCP result Codex wrote out as JSON content."""
    try:
        parts = json.loads(body)
    except ValueError:
        return body
    if not isinstance(parts, list):
        return body
    return "\n".join(p.get("text", "") for p in parts if isinstance(p, dict))


def tool_error(tool: dict, output: str) -> Optional[str]:
    """error.type for a failed call, None for one that succeeded.

    A command that exited non-zero is `<tool>.exit_<code>`, as spans.py types
    a Claude Code Bash failure; an MCP call its server marked as an error is
    `<tool>.tool_error`. Anything else reads as success: Codex has no generic
    error flag on a tool's output.
    """
    match = _EXIT_CODE.search(output.partition(_OUTPUT_MARKER)[0])
    if match and match.group(1) != "0":
        return "%s.exit_%s" % (tool["tool_name"], match.group(1))
    if tool.get("mcp_error") is not None:
        return tool["tool_name"] + ".tool_error"
    return None


def _error_detail(tool: dict, output: str) -> str:
    if tool["mcp_error"]:
        return tool["mcp_error"]
    body = _command_output(output)
    return _mcp_text(body) if tool["mcp_error"] is not None else body


def error_line(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return truncate(lines[-1], ERROR_MESSAGE_MAX_BYTES) if lines else ""


def _on_tool_output(rec, state, ctx, out):
    tool = state["open_tools"].pop(rec.get("call_id"), None)
    if state["turn"] is not None:
        state["turn"]["llm_start_ns"] = rec.timestamp_ns
    if tool is None:
        return      # called before this session was traced
    output = rec.get("output")
    if tool["tool_name"] == SPAWN_TOOL:
        _note_spawned(state, tool, output)
    attrs = _attrs(ctx, "TOOL")
    attrs["gen_ai.tool.name"] = tool["tool_name"]
    _content_attr(ctx, attrs, "input.value", tool["input_json"])
    _content_attr(ctx, attrs, "output.value", output)
    error_type = tool_error(tool, output)
    status_message, events = "", []
    if error_type:
        attrs["error.type"] = error_type
        if ctx.capture_content:
            status_message = error_line(_error_detail(tool, output)) or error_type
        else:
            status_message = TOOL_ERROR_WITHHELD
        events.append((rec.timestamp_ns, "exception", {
            "exception.type": error_type, "exception.message": status_message}))
    out.append(_finished(ctx, tool["span_id"], tool["parent_span_id"],
                         tool["tool_name"], "TOOL", tool["start_ns"],
                         rec.timestamp_ns, attrs,
                         "ERROR" if error_type else "OK", status_message, events))


def _note_spawned(state: dict, tool: dict, output: str) -> None:
    """spawn_agent answers with the new agent's id, which names its rollout."""
    try:
        agent_id = json.loads(output).get("agent_id")
    except (ValueError, AttributeError):
        return
    if isinstance(agent_id, str) and agent_id:
        state.setdefault("spawned", {})[agent_id] = tool["span_id"]


def _close_open_tools(state: dict, ctx: Ctx, now_ns: int) -> List[Span]:
    """A call whose output never came (interrupted) still needs a finished
    row: the backend counts a trace only when every span has one."""
    out = []
    for tool in state["open_tools"].values():
        attrs = _attrs(ctx, "TOOL")
        attrs["gen_ai.tool.name"] = tool["tool_name"]
        _content_attr(ctx, attrs, "input.value", tool["input_json"])
        out.append(_finished(ctx, tool["span_id"], tool["parent_span_id"],
                             tool["tool_name"], "TOOL", tool["start_ns"],
                             now_ns, attrs, status_code="UNSET"))
    state["open_tools"] = {}
    return out


def _close_turn(state: dict, ctx: Ctx, end_ns: int, rec) -> List[Span]:
    turn = state["turn"]
    out = _close_open_tools(state, ctx, end_ns)
    attrs = _attrs(ctx, "CHAIN")
    attrs["codex.turn.id"] = turn["turn_id"]
    if rec is not None:
        if rec.get("duration_ms"):
            attrs["codex.turn.duration_ms"] = rec.get("duration_ms")
        if rec.get("ttft_ms"):
            attrs["codex.turn.ttft_ms"] = rec.get("ttft_ms")
        if rec.get("aborted"):
            attrs["codex.turn.aborted"] = rec.get("reason") or "aborted"
        _content_attr(ctx, attrs, "output.value", rec.get("last_agent_message"))
    _content_attr(ctx, attrs, "input.value", turn["text"])
    out.append(_finished(ctx, turn["span_id"], _root_span_id(state, ctx),
                         "turn", "CHAIN", turn["start_ns"], end_ns, attrs))
    state["turn"] = None
    return out


def _on_turn_end(rec, state, ctx, out):
    out += _close_turn(state, ctx, rec.timestamp_ns, rec)


_HANDLERS = {
    cr.SESSION: _on_session,
    cr.TURN_CONTEXT: _on_turn_context,
    cr.TURN_START: _on_turn_start,
    cr.USER_MESSAGE: _on_user_message,
    cr.AGENT_MESSAGE: _on_agent_message,
    cr.USAGE: _on_usage,
    cr.TOOL_CALL: _on_tool_call,
    cr.MCP_RESULT: _on_mcp_result,
    cr.TOOL_OUTPUT: _on_tool_output,
    cr.TURN_END: _on_turn_end,
}

# Kinds that only mean something inside a turn. One seen with no turn open
# began before this session was traced, and is dropped.
_NEEDS_TURN = {cr.USER_MESSAGE, cr.AGENT_MESSAGE, cr.USAGE, cr.TOOL_CALL,
               cr.TURN_END}


def _root_attrs(state: dict) -> Dict[str, Any]:
    return {k: v for k, v in (state.get("root_attrs") or {}).items() if v}


def _root(state: dict, ctx: Ctx, start_ns: int) -> Span:
    return _pending(ctx, _root_span_id(state, ctx), state.get("root_parent"),
                    state.get("root_name") or DEFAULT_ROOT_NAME, "AGENT",
                    start_ns, _root_attrs(state))


def _adopt_role(state: dict, records: List[Any]) -> None:
    """A subagent's rollout names its role, which its hooks may not have
    told us before its root span goes out."""
    if not state.get("root_id"):
        return
    for rec in records:
        if rec.kind == cr.SESSION and rec.get("agent_role"):
            state["root_name"] = rec.get("agent_role")
            state["root_attrs"]["gen_ai.agent.name"] = rec.get("agent_role")
            return


def build(records: List[Any], state: dict, ctx: Ctx) -> List[Span]:
    """Records read since the last call -> the spans they open or finish."""
    out: List[Span] = []
    if records:
        state["finalized"] = False
    if records and not state["root_started"]:
        _adopt_role(state, records)
        state["root_started"] = True
        state["root_start_ns"] = records[0].timestamp_ns
        out.append(_root(state, ctx, records[0].timestamp_ns))
    for rec in records:
        if rec.kind in _NEEDS_TURN and state["turn"] is None:
            continue
        _HANDLERS[rec.kind](rec, state, ctx, out)
    return out


def finalize_session(state: dict, ctx: Ctx, now_ns: int) -> List[Span]:
    """Close everything still open, so no span of the trace stays pending."""
    out: List[Span] = []
    if state["turn"] is not None:
        out += _close_turn(state, ctx, now_ns, None)
    state["finalized"] = True
    if not state["root_started"]:
        return out
    attrs = _attrs(ctx, "AGENT")
    attrs.update(_root_attrs(state))
    out.append(_finished(ctx, _root_span_id(state, ctx),
                         state.get("root_parent"),
                         state.get("root_name") or DEFAULT_ROOT_NAME,
                         "AGENT", state["root_start_ns"] or now_ns, now_ns,
                         attrs))
    return out


def resource_attributes(state: dict) -> Dict[str, str]:
    """The session's codex.* resource attributes, beside service.name."""
    session = state.get("session") or {}
    return {
        "codex.version": session.get("cli_version", ""),
        "codex.cwd": session.get("cwd", ""),
        "codex.git_branch": session.get("git_branch", ""),
        "codex.originator": session.get("originator", ""),
    }
