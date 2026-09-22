"""Transcript entries -> OTLP GenAI span tree.

Knows nothing about JSONL parsing (that's transcript.py) or OTLP encoding
(that's a later task). This module only decides what spans exist, their
ids, their nesting, and their attributes.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional

PROVIDER_NAME = "anthropic"

_OPERATION_BY_KIND = {
    "AGENT": "invoke_agent",
    "LLM": "chat",
    "TOOL": "execute_tool",
    "CHAIN": None,
}

# Allowlist: the ONLY attribute keys a pending span may carry, plus any key
# prefixed with "gen_ai.request.". This is deliberately an allowlist, not a
# blocklist -- a pending span is emitted before work finishes and must never
# leak content such as input.value / output.value.
_PENDING_ALLOWED_KEYS = {
    "openinference.span.kind",
    "gen_ai.operation.name",
    "gen_ai.provider.name",
    "gen_ai.tool.name",
    "session.id",
    "glassflow.span.pending",
}


def _filter_pending_attrs(attributes: Dict[str, Any]) -> Dict[str, Any]:
    out = {}
    for k, v in attributes.items():
        if k in _PENDING_ALLOWED_KEYS or k.startswith("gen_ai.request."):
            out[k] = v
    return out


def trace_id_for(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]


def span_id_for(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def truncate(value: str, limit: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value
    dropped = len(encoded) - limit
    return value[:limit] + " …[truncated %d bytes]" % dropped


class Ctx:
    """Plain, mutable context. Tests mutate `capture_content` post-construction."""

    def __init__(self, session_id, cwd, git_branch, cc_version, service_name,
                 capture_content, max_attr_bytes):
        self.session_id = session_id
        self.cwd = cwd
        self.git_branch = git_branch
        self.cc_version = cc_version
        self.service_name = service_name
        self.capture_content = capture_content
        self.max_attr_bytes = max_attr_bytes


class Span:
    def __init__(self, trace_id, span_id, parent_span_id, name, kind_oi,
                 start_ns, end_ns, attributes, status_code, status_message,
                 pending):
        self.trace_id = trace_id
        self.span_id = span_id
        self.parent_span_id = parent_span_id
        self.name = name
        self.kind_oi = kind_oi
        self.start_ns = start_ns
        self.end_ns = end_ns
        self.attributes = _filter_pending_attrs(attributes) if pending else dict(attributes)
        self.status_code = status_code
        self.status_message = status_message
        self.pending = pending


def _base_attrs(ctx: Ctx, kind_oi: str) -> Dict[str, Any]:
    attrs: Dict[str, Any] = {
        "openinference.span.kind": kind_oi,
        "session.id": ctx.session_id,
    }
    op = _OPERATION_BY_KIND.get(kind_oi)
    if op is not None:
        attrs["gen_ai.operation.name"] = op
    if kind_oi in ("LLM", "TOOL", "AGENT"):
        attrs["gen_ai.provider.name"] = PROVIDER_NAME
    return attrs


def _content_attr(ctx: Ctx, attrs: Dict[str, Any], key: str, value: str) -> None:
    if not ctx.capture_content:
        return
    attrs[key] = truncate(value, ctx.max_attr_bytes)


def _current_turn_parent(state: dict, root_span_id: str) -> str:
    open_turns = state["open_turns"]
    if open_turns:
        last_key = next(reversed(open_turns))
        return open_turns[last_key]["span_id"]
    return root_span_id


def build(entries: List[Any], state: dict, ctx: Ctx) -> List[Any]:
    out: List[Span] = []
    trace_id = trace_id_for(ctx.session_id)
    root_span_id = span_id_for("session:" + ctx.session_id)

    for entry in entries:
        if not state["root_started"]:
            state["root_started"] = True
            state["root_start_ns"] = entry.timestamp_ns
            attrs = _base_attrs(ctx, "AGENT")
            attrs["glassflow.span.pending"] = True
            out.append(Span(
                trace_id=trace_id, span_id=root_span_id, parent_span_id=None,
                name="claude-code session", kind_oi="AGENT",
                start_ns=entry.timestamp_ns, end_ns=entry.timestamp_ns,
                attributes=attrs, status_code="UNSET", status_message="",
                pending=True,
            ))

        if entry.kind == "user":
            tool_results = entry.tool_results()
            if tool_results:
                for tr in tool_results:
                    tool_use_id = tr.get("tool_use_id")
                    open_tool = state["open_tools"].pop(tool_use_id, None)
                    if open_tool is None:
                        continue  # tool started before instrumentation was enabled
                    if open_tool["span_id"] in state["open_task_spans"]:
                        state["open_task_spans"].remove(open_tool["span_id"])
                    is_error = bool(tr.get("is_error"))
                    content = tr.get("content")
                    content_str = content if isinstance(content, str) else json.dumps(content)
                    attrs = _base_attrs(ctx, "TOOL")
                    attrs["gen_ai.tool.name"] = open_tool["tool_name"]
                    _content_attr(ctx, attrs, "input.value", open_tool["input_json"])
                    _content_attr(ctx, attrs, "output.value", content_str)
                    status_code = "ERROR" if is_error else "OK"
                    # Status.message is content too: for a failed Bash call
                    # it is the command's stdout+stderr. It must honour the
                    # capture gate exactly like input.value/output.value do,
                    # or RIUS_CAPTURE_CONTENT=false is not the guarantee the
                    # README makes it out to be.
                    if not is_error:
                        status_message = ""
                    elif ctx.capture_content:
                        status_message = truncate(content_str, ctx.max_attr_bytes)
                    else:
                        status_message = "tool error"
                    out.append(Span(
                        trace_id=trace_id, span_id=open_tool["span_id"],
                        parent_span_id=open_tool["parent_span_id"], name=open_tool["tool_name"],
                        kind_oi="TOOL", start_ns=open_tool["start_ns"], end_ns=entry.timestamp_ns,
                        attributes=attrs, status_code=status_code, status_message=status_message,
                        pending=False,
                    ))
            else:
                if entry.prompt_id and entry.prompt_id not in state["open_turns"]:
                    turn_span_id = span_id_for("turn:" + entry.prompt_id)
                    state["open_turns"][entry.prompt_id] = {
                        "span_id": turn_span_id,
                        "parent_span_id": root_span_id,
                        "start_ns": entry.timestamp_ns,
                        # state.save writes this dict to
                        # ~/.claude/rius/state/<sid>.json in plaintext, so
                        # keeping the prompt here would persist it to disk
                        # even with capture off. It is only ever read back to
                        # fill input.value, which the gate drops anyway.
                        "text": entry.text() if ctx.capture_content else "",
                    }
                    attrs = _base_attrs(ctx, "CHAIN")
                    attrs["glassflow.span.pending"] = True
                    out.append(Span(
                        trace_id=trace_id, span_id=turn_span_id, parent_span_id=root_span_id,
                        name="turn", kind_oi="CHAIN", start_ns=entry.timestamp_ns,
                        end_ns=entry.timestamp_ns, attributes=attrs, status_code="UNSET",
                        status_message="", pending=True,
                    ))

        elif entry.kind == "assistant":
            if entry.is_sidechain and state["open_task_spans"]:
                parent_span_id = state["open_task_spans"][-1]
            else:
                parent_span_id = _current_turn_parent(state, root_span_id)

            model = entry.message.get("model") or ""
            usage = entry.message.get("usage") or {}
            stop_reason = entry.message.get("stop_reason")
            llm_span_id = span_id_for(entry.uuid)
            start_ns = state["last_ns"] or entry.timestamp_ns

            attrs = _base_attrs(ctx, "LLM")
            if model:
                attrs["gen_ai.request.model"] = model
                attrs["gen_ai.response.model"] = model
            if "input_tokens" in usage:
                attrs["gen_ai.usage.input_tokens"] = usage["input_tokens"]
            if "output_tokens" in usage:
                attrs["gen_ai.usage.output_tokens"] = usage["output_tokens"]
            if "cache_read_input_tokens" in usage:
                attrs["gen_ai.usage.cache_read.input_tokens"] = usage["cache_read_input_tokens"]
            if "cache_creation_input_tokens" in usage:
                attrs["gen_ai.usage.cache_creation.input_tokens"] = usage["cache_creation_input_tokens"]
            if stop_reason:
                attrs["gen_ai.response.finish_reasons"] = [stop_reason]
            _content_attr(ctx, attrs, "output.value", entry.text())

            out.append(Span(
                trace_id=trace_id, span_id=llm_span_id, parent_span_id=parent_span_id,
                name=model or "assistant", kind_oi="LLM", start_ns=start_ns,
                end_ns=entry.timestamp_ns, attributes=attrs, status_code="OK",
                status_message="", pending=False,
            ))

            for block in entry.tool_uses():
                tool_id = block.get("id")
                tool_name = block.get("name") or ""
                tool_span_id = span_id_for(tool_id)
                input_json = json.dumps(block.get("input") or {})
                state["open_tools"][tool_id] = {
                    "span_id": tool_span_id,
                    "parent_span_id": llm_span_id,
                    "start_ns": entry.timestamp_ns,
                    "tool_name": tool_name,
                    "input_json": input_json,
                }
                if tool_name == "Task":
                    state["open_task_spans"].append(tool_span_id)

                attrs = _base_attrs(ctx, "TOOL")
                attrs["gen_ai.tool.name"] = tool_name
                attrs["glassflow.span.pending"] = True
                out.append(Span(
                    trace_id=trace_id, span_id=tool_span_id, parent_span_id=llm_span_id,
                    name=tool_name, kind_oi="TOOL", start_ns=entry.timestamp_ns,
                    end_ns=entry.timestamp_ns, attributes=attrs, status_code="UNSET",
                    status_message="", pending=True,
                ))

        state["last_ns"] = entry.timestamp_ns

    return out


def finalize_turn(state: dict, ctx: Ctx, now_ns: int) -> List[Any]:
    out: List[Span] = []
    trace_id = trace_id_for(ctx.session_id)
    for prompt_id, turn in list(state["open_turns"].items()):
        attrs = _base_attrs(ctx, "CHAIN")
        _content_attr(ctx, attrs, "input.value", turn.get("text", ""))
        out.append(Span(
            trace_id=trace_id, span_id=turn["span_id"], parent_span_id=turn["parent_span_id"],
            name="turn", kind_oi="CHAIN", start_ns=turn["start_ns"], end_ns=now_ns,
            attributes=attrs, status_code="OK", status_message="", pending=False,
        ))
    state["open_turns"] = {}
    return out


def finalize_session(state: dict, ctx: Ctx, now_ns: int) -> List[Any]:
    out = finalize_turn(state, ctx, now_ns)
    if not state.get("root_started"):
        # build() never saw an entry: the user opened a session in an enabled
        # folder, typed nothing and quit. There is no root span to close --
        # closing one anyway emits a span starting at root_start_ns == 0, the
        # Unix epoch, which draws as a 56-year bar.
        return out
    trace_id = trace_id_for(ctx.session_id)
    root_span_id = span_id_for("session:" + ctx.session_id)
    attrs = _base_attrs(ctx, "AGENT")
    out.append(Span(
        trace_id=trace_id, span_id=root_span_id, parent_span_id=None,
        name="claude-code session", kind_oi="AGENT",
        start_ns=state.get("root_start_ns") or now_ns,
        end_ns=now_ns, attributes=attrs, status_code="OK", status_message="",
        pending=False,
    ))
    return out
