"""Codex rollout JSONL -> neutral Record objects.

A rollout (`$CODEX_HOME/sessions/YYYY/MM/DD/rollout-*.jsonl`) is one
`{timestamp, type, payload}` object per line. Only the lines that say what
happened are turned into records; prompts Codex injects, world state and the
like are skipped. Knows nothing about spans (that's codex_spans.py).
"""
from __future__ import annotations

import json
import os
from typing import Any, Callable, Dict, List, Optional, Tuple

from .transcript import _timestamp_ns

SESSION = "session"
TURN_START = "turn_start"
TURN_CONTEXT = "turn_context"
TURN_END = "turn_end"
USER_MESSAGE = "user_message"
AGENT_MESSAGE = "agent_message"
USAGE = "usage"
TOOL_CALL = "tool_call"
TOOL_OUTPUT = "tool_output"
MCP_RESULT = "mcp_result"


class Record:
    __slots__ = ("kind", "timestamp_ns", "offset", "fields")

    def __init__(self, kind: str, timestamp_ns: int, fields: Dict[str, Any]) -> None:
        self.kind = kind
        self.timestamp_ns = timestamp_ns
        self.fields = fields
        self.offset = -1

    def get(self, key: str, default: Any = None) -> Any:
        return self.fields.get(key, default)


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _session(p: Dict[str, Any]) -> Dict[str, Any]:
    git = p.get("git") if isinstance(p.get("git"), dict) else {}
    return {
        "thread_id": _text(p.get("id") or p.get("session_id")),
        "cwd": _text(p.get("cwd")),
        "cli_version": _text(p.get("cli_version")),
        "originator": _text(p.get("originator")),
        "model_provider": _text(p.get("model_provider")),
        "git_branch": _text(git.get("branch")),
        "agent_role": _text(p.get("agent_role")),
    }


def _turn_context(p: Dict[str, Any]) -> Dict[str, Any]:
    return {"turn_id": _text(p.get("turn_id")), "model": _text(p.get("model"))}


def _usage(p: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    info = p.get("info")
    last = info.get("last_token_usage") if isinstance(info, dict) else None
    if not isinstance(last, dict):
        return None         # a rate-limit-only update: no model call behind it
    return {
        "input_tokens": _int(last.get("input_tokens")),
        "cached_input_tokens": _int(last.get("cached_input_tokens")),
        "output_tokens": _int(last.get("output_tokens")),
        "reasoning_output_tokens": _int(last.get("reasoning_output_tokens")),
        "context_window": _int(info.get("model_context_window")),
    }


def _turn_end(p: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "turn_id": _text(p.get("turn_id")),
        "last_agent_message": _text(p.get("last_agent_message")),
        "duration_ms": _int(p.get("duration_ms")),
        "ttft_ms": _int(p.get("time_to_first_token_ms")),
        "aborted": p.get("type") == "turn_aborted",
        "reason": _text(p.get("reason")),
    }


def _mcp_result(p: Dict[str, Any]) -> Dict[str, Any]:
    result = p.get("result") if isinstance(p.get("result"), dict) else {}
    ok = result.get("Ok") if isinstance(result.get("Ok"), dict) else {}
    err = result.get("Err")
    is_error = err is not None or bool(ok.get("isError"))
    return {"call_id": _text(p.get("call_id")), "is_error": is_error,
            "error": _text(err)}


_EVENTS: Dict[str, Tuple[str, Callable[[Dict[str, Any]], Optional[Dict[str, Any]]]]] = {
    "task_started": (TURN_START, lambda p: {"turn_id": _text(p.get("turn_id"))}),
    "task_complete": (TURN_END, _turn_end),
    "turn_aborted": (TURN_END, _turn_end),
    "user_message": (USER_MESSAGE, lambda p: {"text": _text(p.get("message"))}),
    "agent_message": (AGENT_MESSAGE, lambda p: {"text": _text(p.get("message"))}),
    "token_count": (USAGE, _usage),
    "mcp_tool_call_end": (MCP_RESULT, _mcp_result),
}


def _tool_call(p: Dict[str, Any]) -> Dict[str, Any]:
    name = _text(p.get("name"))
    namespace = _text(p.get("namespace"))
    # An MCP tool is called as `name` inside its server's namespace
    # ("mcp__<server>"); joined the way Claude Code spells MCP tools.
    full_name = namespace + "__" + name if namespace else name
    arguments = p.get("arguments") if p.get("type") == "function_call" else p.get("input")
    return {"call_id": _text(p.get("call_id")), "name": full_name,
            "arguments": _text(arguments)}


def _tool_output(p: Dict[str, Any]) -> Dict[str, Any]:
    output = p.get("output")
    if not isinstance(output, str):
        output = json.dumps(output)
    return {"call_id": _text(p.get("call_id")), "output": output}


_ITEMS = {
    "function_call": (TOOL_CALL, _tool_call),
    "custom_tool_call": (TOOL_CALL, _tool_call),
    "function_call_output": (TOOL_OUTPUT, _tool_output),
    "custom_tool_call_output": (TOOL_OUTPUT, _tool_output),
}


def _fields_for(line_type: str, payload: Dict[str, Any]):
    if line_type == "session_meta":
        return SESSION, _session(payload)
    if line_type == "turn_context":
        return TURN_CONTEXT, _turn_context(payload)
    table = _EVENTS if line_type == "event_msg" else _ITEMS if line_type == "response_item" else {}
    kind, parse = table.get(payload.get("type"), (None, None))
    if kind is None:
        return None, None
    return kind, parse(payload)


def parse_line(line: str) -> Optional[Record]:
    """A Record, or None for a line that is not one we use or cannot read."""
    try:
        raw = json.loads(line)
    except ValueError:
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("payload"), dict):
        return None
    kind, fields = _fields_for(_text(raw.get("type")), raw["payload"])
    if kind is None or fields is None:
        return None
    try:
        return Record(kind, _timestamp_ns(_text(raw.get("timestamp"))), fields)
    except (ValueError, IndexError):
        return None


def read_from(path: str, offset: int) -> Tuple[List[Record], int]:
    """Read complete lines from `offset`. Returns (records, new_offset).

    A trailing partial line is left unconsumed: Codex is still writing it, and
    half a JSON object must not move the offset past itself.
    """
    try:
        if offset > os.path.getsize(path):
            offset = 0      # the file was replaced; start over
        with open(path, "rb") as fh:
            fh.seek(offset)
            buf = fh.read()
    except OSError:
        return [], offset
    records: List[Record] = []
    start = 0
    while True:
        nl = buf.find(b"\n", start)
        if nl == -1:
            break
        line_offset = offset + start
        record = parse_line(buf[start:nl].decode("utf-8", errors="replace"))
        start = nl + 1
        if record is not None:
            record.offset = line_offset
            records.append(record)
    return records, offset + start
