"""Cursor hook events (cursor_events.py) -> the span tree spans.py emits.

One trace per conversation:

    AGENT  cursor session            sessionStart .. sessionEnd
      CHAIN  turn                    one per generation_id, prompt .. stop
        LLM    <model>               the generation's answer, no token usage
        TOOL   <tool_name>           preToolUse .. postToolUse(Failure)
          AGENT  <subagent_type>     subagentStart .. subagentStop, under
                                     the Task call that spawned it

Every span is rebuilt from the whole spool each time, with ids and start
times derived from the events, so a span sent pending and later finished
is the same row. Cursor reports no token counts, so no LLM span has any.
"""
from __future__ import annotations

import json
import re
from collections import OrderedDict
from typing import Any, Dict, List, Optional

from . import scrub
from .spans import (ERROR_MESSAGE_MAX_BYTES, TOOL_ERROR_WITHHELD, Span,
                    exportable, span_id_for, tool_error_line, trace_id_for)

SERVICE_NAME = "cursor"
DEFAULT_ROOT_NAME = "cursor session"
SESSION_ERROR_WITHHELD = "session error (detail withheld: RIUS_CAPTURE_CONTENT=false)"

_OPERATION_BY_KIND = {"AGENT": "invoke_agent", "LLM": "chat",
                      "TOOL": "execute_tool", "CHAIN": None}

# Model-name prefix -> OTel gen_ai.provider.name. Cursor's own models
# ("auto", "composer-1", "cheetah") and anything unknown are "cursor".
_PROVIDER_PREFIXES = (
    ("claude", "anthropic"), ("anthropic", "anthropic"),
    ("sonnet", "anthropic"), ("opus", "anthropic"), ("haiku", "anthropic"),
    ("gpt", "openai"), ("o1", "openai"), ("o3", "openai"), ("o4", "openai"),
    ("codex", "openai"), ("openai", "openai"),
    ("gemini", "gcp.gemini"), ("grok", "x_ai"), ("deepseek", "deepseek"),
    ("kimi", "moonshot"), ("mistral", "mistral_ai"),
)

_NO_MODEL = ("", "unknown", "default")

# cursor-agent names each model step of a turn `<turn id>-<step>-<random>`
# in afterAgentThought; the turn itself is the bare id.
_STEP_OF_TURN = re.compile(
    r"^([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
    r"-[0-9]+-[0-9A-Za-z]+$")


def turn_key(generation_id: str) -> str:
    match = _STEP_OF_TURN.match(generation_id)
    return match.group(1) if match else generation_id
_TURN_STATUS = {"completed": "OK", "error": "ERROR"}


def provider_for(model: str) -> str:
    name = (model or "").strip().lower()
    for prefix, provider in _PROVIDER_PREFIXES:
        if name.startswith(prefix):
            return provider
    return "cursor"


class Ctx:
    def __init__(self, conversation_id: str, capture_content: bool,
                 max_attr_bytes: int):
        self.conversation_id = conversation_id
        self.capture_content = capture_content
        self.max_attr_bytes = max_attr_bytes


def _model_of(event: Dict[str, Any]) -> str:
    model = event.get("model") or ""
    return "" if model in _NO_MODEL else str(model)


def _base_attrs(ctx: Ctx, kind_oi: str, model: str = "") -> Dict[str, Any]:
    attrs: Dict[str, Any] = {"openinference.span.kind": kind_oi,
                             "session.id": ctx.conversation_id}
    op = _OPERATION_BY_KIND[kind_oi]
    if op:
        attrs["gen_ai.operation.name"] = op
    if kind_oi != "CHAIN":
        attrs["gen_ai.provider.name"] = provider_for(model)
    return attrs


def _content(ctx: Ctx, attrs: Dict[str, Any], key: str, value: Any) -> None:
    if not ctx.capture_content or value in (None, "", [], {}):
        return
    attrs[key] = exportable(_as_text(value), ctx.max_attr_bytes)


def _as_text(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value)


def _set(attrs: Dict[str, Any], key: str, value: Any) -> None:
    if value is not None and value != "":
        attrs[key] = value


class _Fold:
    """What the events say about each span, gathered in one pass."""

    def __init__(self, conversation_id: str):
        self.cid = conversation_id
        self.root: Dict[str, Any] = {}
        self.turns: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
        self.tools: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
        self.subagents: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
        self.last_ns = 0
        # Bumped by each sessionEnd: `cursor-agent -p --resume` runs one
        # prompt per process, each closed by its own sessionEnd.
        self.segment = 0

    def note_scope(self, event: Dict[str, Any]) -> None:
        """A linked subagent that fired no subagentStart (cursor-agent -p)
        gets its span from its first event, under the latest Task call."""
        cid = str(event.get("conversation_id") or "")
        if not cid or cid == self.cid or event.get("event") in (
                "subagentStart", "subagentStop"):
            return
        sub = self.subagents.get(cid)
        if sub is None:
            task = self._unclaimed_task()
            sub = {"id": cid, "start_ns": event["ts"], "end_ns": None,
                   "type": (task or {}).get("subagent_type") or "",
                   "model": "", "tool_call_id": task["id"] if task else "",
                   "turn": task["turn"] if task else None, "task": None,
                   "stop": {}, "implicit": True, "last_ns": event["ts"],
                   "segment": self.segment}
            self.subagents[cid] = sub
        if sub.get("implicit"):
            sub["last_ns"] = event["ts"]
            sub["model"] = sub["model"] or _model_of(event)

    def _unclaimed_task(self) -> Optional[Dict[str, Any]]:
        """The Task call a new subagent belongs to: the oldest one of this
        run still open, as parallel Tasks start their subagents in the
        order they were called; else the latest one."""
        claimed = {s["tool_call_id"] for s in self.subagents.values()}
        tasks = [tool for tool in self.tools.values()
                 if tool["name"] == "Task" and tool["scope"] == self.cid
                 and tool["key"] not in claimed]
        running = [tool for tool in tasks if tool["end_ns"] is None
                   and tool["segment"] == self.segment]
        return (running or tasks[-1:] or [None])[0]

    def scope_of(self, event: Dict[str, Any]) -> str:
        """The conversation an event ran in: ours, or a subagent's."""
        cid = str(event.get("conversation_id") or "")
        if cid and cid in self.subagents and event.get("event") not in (
                "subagentStart", "subagentStop"):
            return cid
        return self.cid

    def turn_for(self, event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """The turn an event belongs to. A subagent's work hangs straight
        under its own span, as in Claude Code: it has no turns."""
        gen = str(event.get("generation_id") or "")
        if (not gen or self.scope_of(event) != self.cid
                or gen in (self.cid, event.get("session_id"))):
            return None
        key = turn_key(gen)
        turn = self.turns.get(key)
        if turn is None:
            turn = {"key": key, "start_ns": event["ts"],
                    "end_ns": None, "prompt": None, "status": "",
                    "loop_count": None, "model": "", "model_id": "",
                    "texts": [], "response_ns": 0, "thought_ms": 0,
                    "context": {}, "segment": self.segment}
            self.turns[key] = turn
        turn["model"] = _model_of(event) or turn["model"]
        turn["model_id"] = str(event.get("model_id") or "") or turn["model_id"]
        return turn


def _on_session_start(fold: _Fold, e: Dict[str, Any]) -> None:
    fold.root.setdefault("start_ns", e["ts"])
    fold.root["composer_mode"] = e.get("composer_mode")
    fold.root["background"] = e.get("is_background_agent")
    fold.root["model"] = _model_of(e) or fold.root.get("model", "")


def _on_session_end(fold: _Fold, e: Dict[str, Any]) -> None:
    fold.root["end_ns"] = e["ts"]
    for key in ("reason", "final_status", "duration_ms", "error_message"):
        fold.root[key] = e.get(key)
    _close_segment(fold, e)
    fold.segment += 1


def _close_segment(fold: _Fold, e: Dict[str, Any]) -> None:
    """End what this process left open: in cursor-agent -p no stop ends a
    turn, a Task call gets no postToolUse, and a subagent no subagentStop.
    Left open, a later --resume of the conversation would stretch them."""
    for sub in fold.subagents.values():
        if sub["end_ns"] is None and sub.get("segment") == fold.segment:
            sub["end_ns"] = sub.get("last_ns") or e["ts"]
            task = fold.tools.get(sub["tool_call_id"])
            if task is not None and task["end_ns"] is None:
                task["end_ns"] = sub["end_ns"]
                task["closed_at_session_end"] = True
    for tool in fold.tools.values():
        if tool["end_ns"] is None and tool["segment"] == fold.segment:
            tool["end_ns"] = e["ts"]
            tool["closed_at_session_end"] = True
    for turn in fold.turns.values():
        if turn["end_ns"] is None and turn["segment"] == fold.segment:
            turn["end_ns"] = e["ts"]
            turn["status"] = turn["status"] or str(e.get("final_status") or "")


def _on_prompt(fold: _Fold, e: Dict[str, Any]) -> None:
    turn = fold.turn_for(e)
    if turn is not None:
        turn["prompt"] = e.get("prompt")


def _on_response(fold: _Fold, e: Dict[str, Any]) -> None:
    turn = fold.turn_for(e)
    if turn is not None:
        turn["texts"].append(e.get("text") or "")
        turn["response_ns"] = e["ts"]


def _on_thought(fold: _Fold, e: Dict[str, Any]) -> None:
    turn = fold.turn_for(e)
    if turn is not None and isinstance(e.get("duration_ms"), int):
        turn["thought_ms"] += e["duration_ms"]


def _on_stop(fold: _Fold, e: Dict[str, Any]) -> None:
    turn = fold.turn_for(e)
    if turn is not None:
        turn["end_ns"] = e["ts"]
        turn["status"] = e.get("status") or ""
        turn["loop_count"] = e.get("loop_count")


def _on_compact(fold: _Fold, e: Dict[str, Any]) -> None:
    turn = fold.turn_for(e)
    if turn is None:
        return
    for key in ("context_tokens", "context_window_size",
                "context_usage_percent"):
        if e.get(key) is not None:
            turn["context"][key] = e[key]


def _tool_key(fold: _Fold, e: Dict[str, Any]) -> str:
    """cursor-agent can hand two calls one tool_use_id (a Read and the
    Write after it); the tool name tells them apart."""
    tool_id = str(e.get("tool_use_id") or "")
    name = str(e.get("tool_name") or "")
    known = fold.tools.get(tool_id)
    if tool_id and name and known is not None and known["name"] != name:
        return "%s#%s" % (tool_id, name)
    return tool_id


def _tool(fold: _Fold, e: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    key = _tool_key(fold, e)
    if not key:
        return None
    tool = fold.tools.get(key)
    if tool is None:
        turn = fold.turn_for(e)
        tool = {"key": key, "id": str(e.get("tool_use_id")),
                "name": str(e.get("tool_name") or "tool"),
                "scope": fold.scope_of(e),
                "turn": turn["key"] if turn else None,
                "segment": fold.segment, "start_ns": None, "end_ns": None,
                "input": None, "output": None, "failure": None, "error": None,
                "interrupted": False, "exit_code": None, "model": _model_of(e),
                "subagent_type": e.get("subagent_type") or "",
                "closed_at_session_end": False}
        fold.tools[key] = tool
    if e.get("tool_input") is not None:
        tool["input"] = e["tool_input"]
    return tool


def _on_pre_tool(fold: _Fold, e: Dict[str, Any]) -> None:
    tool = _tool(fold, e)
    if tool is not None:
        tool["start_ns"] = e["ts"]


def _close_tool(tool: Dict[str, Any], e: Dict[str, Any]) -> None:
    tool["end_ns"] = e["ts"]
    if tool["start_ns"] is None:
        # No preToolUse seen (the hook was added mid-call): Cursor's own
        # duration puts the start back where it was.
        duration = e.get("duration")
        ms = duration if isinstance(duration, (int, float)) else 0
        tool["start_ns"] = e["ts"] - int(ms * 1000000)


def _on_post_tool(fold: _Fold, e: Dict[str, Any]) -> None:
    tool = _tool(fold, e)
    if tool is not None:
        _close_tool(tool, e)
        tool["output"] = e.get("tool_output")
        tool["exit_code"] = e.get("exit_code")


def _on_tool_failure(fold: _Fold, e: Dict[str, Any]) -> None:
    tool = _tool(fold, e)
    if tool is not None:
        _close_tool(tool, e)
        tool["failure"] = e.get("failure_type") or "error"
        tool["error"] = e.get("error_message")
        tool["interrupted"] = bool(e.get("is_interrupt"))


def _on_subagent_start(fold: _Fold, e: Dict[str, Any]) -> None:
    sub_id = str(e.get("subagent_id") or "")
    if not sub_id:
        return
    turn = fold.turn_for(e)
    fold.subagents[sub_id] = {
        "id": sub_id, "start_ns": e["ts"], "end_ns": None,
        "type": e.get("subagent_type") or "",
        "model": e.get("subagent_model") or "",
        "tool_call_id": str(e.get("tool_call_id") or ""),
        "turn": turn["key"] if turn else None,
        "task": e.get("task"), "stop": {}, "segment": fold.segment,
    }


def _on_subagent_stop(fold: _Fold, e: Dict[str, Any]) -> None:
    sub = fold.subagents.get(str(e.get("subagent_id") or ""))
    if sub is not None:
        sub["end_ns"] = e["ts"]
        sub["stop"] = e


_HANDLERS = {
    "sessionStart": _on_session_start,
    "sessionEnd": _on_session_end,
    "beforeSubmitPrompt": _on_prompt,
    "afterAgentResponse": _on_response,
    "afterAgentThought": _on_thought,
    "stop": _on_stop,
    "preCompact": _on_compact,
    "preToolUse": _on_pre_tool,
    "postToolUse": _on_post_tool,
    "postToolUseFailure": _on_tool_failure,
    "subagentStart": _on_subagent_start,
    "subagentStop": _on_subagent_stop,
}


def _fold_events(events: List[Dict[str, Any]], conversation_id: str) -> _Fold:
    fold = _Fold(conversation_id)
    for e in events:
        if not fold.root.get("start_ns"):
            fold.root["start_ns"] = e["ts"]
        fold.last_ns = max(fold.last_ns, e["ts"])
        fold.note_scope(e)
        handler = _HANDLERS.get(e.get("event") or "")
        if handler is not None:
            handler(fold, e)
        if not fold.root.get("model"):
            fold.root["model"] = _model_of(e)
            fold.root["model_id"] = str(e.get("model_id") or "")
    _adopt_orphan_tools(fold)
    return fold


def _adopt_orphan_tools(fold: _Fold) -> None:
    """cursor-agent stamps tool hooks with the conversation id, not the
    turn's generation id, so a tool would hang off the session. It belongs
    to the turn of the same process that had started by then, or to that
    process's first turn when the tool's hook fired before any thought."""
    for tool in fold.tools.values():
        if tool["turn"] is not None or tool["scope"] != fold.cid:
            continue
        turns = [t for t in fold.turns.values()
                 if t["segment"] == tool["segment"]]
        if not turns:
            continue
        start = tool["start_ns"] if tool["start_ns"] is not None else 0
        started = [t for t in turns if t["start_ns"] <= start]
        turn = started[-1] if started else turns[0]
        tool["turn"] = turn["key"]
        turn["start_ns"] = min(turn["start_ns"], start or turn["start_ns"])


class _Ids:
    def __init__(self, cid: str):
        self.trace = trace_id_for(cid)
        self.root = span_id_for("session:" + cid)

    @staticmethod
    def turn(key: str) -> str:
        return span_id_for("turn:" + key)

    @staticmethod
    def llm(key: str) -> str:
        return span_id_for("generation:" + key)

    @staticmethod
    def subagent(sub_id: str) -> str:
        return span_id_for("subagent:" + sub_id)


def _span(ids: _Ids, span_id: str, parent: Optional[str], name: str,
          kind: str, start_ns: int, end_ns: Optional[int],
          attrs: Dict[str, Any], status: str = "OK", message: str = "",
          events=None) -> Span:
    """A finished span, or a pending one when `end_ns` is None."""
    pending = end_ns is None
    if pending:
        attrs["glassflow.span.pending"] = True
    return Span(trace_id=ids.trace, span_id=span_id, parent_span_id=parent,
                name=name, kind_oi=kind, start_ns=start_ns,
                end_ns=start_ns if pending else end_ns, attributes=attrs,
                status_code="UNSET" if pending else status,
                status_message="" if pending else message, pending=pending,
                events=events)


def _scope_parent(fold: _Fold, ids: _Ids, scope: str) -> str:
    return ids.root if scope == fold.cid else ids.subagent(scope)


def _root_span(fold: _Fold, ctx: Ctx, ids: _Ids, end_ns: Optional[int]) -> Span:
    root = fold.root
    attrs = _base_attrs(ctx, "AGENT",
                        root.get("model_id") or root.get("model") or "")
    _set(attrs, "cursor.composer_mode", root.get("composer_mode"))
    _set(attrs, "cursor.background", root.get("background"))
    _set(attrs, "cursor.session.reason", root.get("reason"))
    _set(attrs, "cursor.session.final_status", root.get("final_status"))
    status, message = "OK", ""
    if root.get("reason") == "error":
        status = "ERROR"
        message = (exportable(root.get("error_message") or "session error",
                              ERROR_MESSAGE_MAX_BYTES)
                   if ctx.capture_content else SESSION_ERROR_WITHHELD)
    return _span(ids, ids.root, None, DEFAULT_ROOT_NAME, "AGENT",
                 root["start_ns"], end_ns, attrs, status, message)


def _turn_spans(fold: _Fold, ctx: Ctx, ids: _Ids, turn: Dict[str, Any],
                final_ns: Optional[int]) -> List[Span]:
    end_ns = turn["end_ns"] or final_ns
    attrs = _base_attrs(ctx, "CHAIN")
    _set(attrs, "cursor.turn.status", turn["status"])
    _set(attrs, "cursor.turn.loop_count", turn["loop_count"])
    _content(ctx, attrs, "input.value", turn["prompt"])
    status = _TURN_STATUS.get(turn["status"], "OK" if turn["end_ns"] else "UNSET")
    out = [_span(ids, ids.turn(turn["key"]), ids.root, "turn", "CHAIN",
                 turn["start_ns"], end_ns, attrs, status)]
    llm = _llm_span(ctx, ids, turn, end_ns)
    if llm is not None:
        out.append(llm)
    return out


def _llm_span(ctx: Ctx, ids: _Ids, turn: Dict[str, Any],
              turn_end_ns: Optional[int]) -> Optional[Span]:
    """The generation's answer, once there is one or the turn is over."""
    end_ns = turn["response_ns"] or turn_end_ns
    if end_ns is None:
        return None
    model = turn["model"]
    attrs = _base_attrs(ctx, "LLM", turn["model_id"] or model)
    _set(attrs, "gen_ai.request.model", model)
    _set(attrs, "gen_ai.response.model", model)
    if turn["thought_ms"]:
        attrs["cursor.thinking.duration_ms"] = turn["thought_ms"]
    for key, value in turn["context"].items():
        attrs["cursor.context." + key] = value
    _content(ctx, attrs, "output.value", "".join(turn["texts"]))
    status = "ERROR" if turn["status"] == "error" else "OK"
    return _span(ids, ids.llm(turn["key"]), ids.turn(turn["key"]),
                 model or "assistant", "LLM", turn["start_ns"], end_ns, attrs,
                 status)


def tool_error_type(tool: Dict[str, Any]) -> str:
    """`Shell.exit_1`, `MCP:query.timeout`, `Write.permission_denied`:
    a tool name and a reason, never output."""
    if tool["failure"]:
        return "%s.%s" % (tool["name"], tool["failure"])
    return "%s.exit_%d" % (tool["name"], tool["exit_code"])


def _tool_failed(tool: Dict[str, Any]) -> bool:
    if tool["failure"]:
        return not tool["interrupted"]
    return isinstance(tool["exit_code"], int) and tool["exit_code"] != 0


def _shell_output(tool_output: Any) -> str:
    """The command's own output from a Shell tool's `{output, exitCode}`."""
    value = tool_output
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return tool_output
    if isinstance(value, dict) and isinstance(value.get("output"), str):
        return value["output"]
    return value if isinstance(value, str) else json.dumps(value)


def _reads_secret_file(tool: Dict[str, Any]) -> bool:
    return tool["input"] is not None and scrub.reads_secret_file(
        _as_text(tool["input"]))


def _tool_error_message(ctx: Ctx, tool: Dict[str, Any], error_type: str) -> str:
    if not ctx.capture_content:
        return TOOL_ERROR_WITHHELD
    if _reads_secret_file(tool):
        return error_type
    text = tool["error"] if tool["failure"] else _shell_output(tool["output"])
    return tool_error_line(scrub.scrub(text or "")) or error_type


def _tool_parent(fold: _Fold, ids: _Ids, tool: Dict[str, Any]) -> str:
    if tool["turn"]:
        return ids.turn(tool["turn"])
    return _scope_parent(fold, ids, tool["scope"])


def _tool_span(fold: _Fold, ctx: Ctx, ids: _Ids, tool: Dict[str, Any],
               final_ns: Optional[int]) -> Span:
    end_ns = tool["end_ns"] or final_ns
    attrs = _base_attrs(ctx, "TOOL", tool["model"])
    attrs["gen_ai.tool.name"] = tool["name"]
    attrs["gen_ai.tool.call.id"] = tool["id"]
    if tool["interrupted"]:
        attrs["cursor.tool.interrupted"] = True
    if tool["closed_at_session_end"]:
        # No postToolUse came: the outcome is unknown, not OK.
        attrs["cursor.tool.closed_at_session_end"] = True
    _content(ctx, attrs, "input.value", tool["input"])
    _content(ctx, attrs, "output.value", scrub.SECRET_FILE_MARKER
             if tool["output"] and _reads_secret_file(tool) else tool["output"])
    finished = (tool["end_ns"] and not tool["interrupted"]
                and not tool["closed_at_session_end"])
    status, message, events = ("OK" if finished else "UNSET"), "", []
    if _tool_failed(tool):
        error_type = tool_error_type(tool)
        status = "ERROR"
        message = _tool_error_message(ctx, tool, error_type)
        attrs["error.type"] = error_type
        events.append((tool["end_ns"], "exception", {
            "exception.type": error_type, "exception.message": message}))
    start_ns = tool["start_ns"] if tool["start_ns"] is not None else fold.last_ns
    return _span(ids, span_id_for(tool["key"]), _tool_parent(fold, ids, tool),
                 tool["name"], "TOOL", start_ns, end_ns, attrs, status,
                 message, events)


def _subagent_parent(fold: _Fold, ids: _Ids, sub: Dict[str, Any]) -> str:
    task = fold.tools.get(sub["tool_call_id"])
    if task is not None:
        return span_id_for(task["key"])
    if sub["turn"]:
        return ids.turn(sub["turn"])
    return ids.root


def _subagent_span(fold: _Fold, ctx: Ctx, ids: _Ids, sub: Dict[str, Any],
                   final_ns: Optional[int]) -> Span:
    stop = sub["stop"]
    end_ns = sub["end_ns"] or final_ns
    attrs = _base_attrs(ctx, "AGENT", sub["model"])
    _set(attrs, "gen_ai.agent.name", sub["type"])
    _set(attrs, "gen_ai.request.model", sub["model"])
    attrs["cursor.subagent.id"] = sub["id"]
    for key in ("status", "tool_call_count", "message_count"):
        _set(attrs, "cursor.subagent." + key, stop.get(key))
    _content(ctx, attrs, "input.value", sub["task"])
    _content(ctx, attrs, "gen_ai.agent.description", stop.get("description"))
    _content(ctx, attrs, "output.value", stop.get("summary"))
    status = "ERROR" if stop.get("status") == "error" else "OK"
    return _span(ids, ids.subagent(sub["id"]), _subagent_parent(fold, ids, sub),
                 sub["type"] or "subagent", "AGENT", sub["start_ns"], end_ns,
                 attrs, status)


def build(events: List[Dict[str, Any]], ctx: Ctx,
          final_ns: Optional[int] = None) -> List[Span]:
    """Spans for every event so far. Whatever has not finished is sent
    pending, unless `final_ns` is given: then it is closed at that time,
    as on sessionEnd or when a crashed session is swept."""
    if not events:
        return []
    fold = _fold_events(events, ctx.conversation_id)
    ids = _Ids(ctx.conversation_id)
    root_end = fold.root.get("end_ns") or final_ns
    out = [_root_span(fold, ctx, ids, root_end)]
    for turn in fold.turns.values():
        out += _turn_spans(fold, ctx, ids, turn, final_ns)
    out += [_tool_span(fold, ctx, ids, t, final_ns) for t in fold.tools.values()]
    out += [_subagent_span(fold, ctx, ids, s, final_ns)
            for s in fold.subagents.values()]
    return out


def resource_attributes(events: List[Dict[str, Any]], instance_id: str = "",
                        service_name: str = SERVICE_NAME) -> Dict[str, Any]:
    """The OTLP resource for a Cursor conversation's spans."""
    attrs: Dict[str, Any] = {"service.name": service_name}
    _set(attrs, "service.instance.id", instance_id)
    for key, source in (("cursor.version", "cursor_version"),
                        ("cursor.cwd", "cwd"), ("cursor.git_branch", "git_branch")):
        value = next((e[source] for e in events if e.get(source)), None)
        _set(attrs, key, value)
    return attrs
