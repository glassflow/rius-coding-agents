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
SUBAGENT_STARTED = "subagent_started"


class Record:
    """One rollout line. Read from a file, it also knows where: `path` and
    `offset`, the start of its line."""
    __slots__ = ("kind", "timestamp_ns", "offset", "path", "fields")

    def __init__(self, kind: str, timestamp_ns: int, fields: Dict[str, Any]) -> None:
        self.kind = kind
        self.timestamp_ns = timestamp_ns
        self.fields = fields
        self.offset = -1
        self.path = ""

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
        "forked_from_id": _text(p.get("forked_from_id")),
        "parent_thread_id": _parent_thread_id(p),
    }


def _parent_thread_id(p: Dict[str, Any]) -> str:
    """A subagent's rollout names the thread that spawned it."""
    source = p.get("source")
    spawn = source.get("subagent") if isinstance(source, dict) else None
    spawn = spawn.get("thread_spawn") if isinstance(spawn, dict) else None
    return _text(spawn.get("parent_thread_id")) if isinstance(spawn, dict) else ""


def _turn_context(p: Dict[str, Any]) -> Dict[str, Any]:
    return {"turn_id": _text(p.get("turn_id")), "model": _text(p.get("model"))}


def _usage(p: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    info = p.get("info")
    last = info.get("last_token_usage") if isinstance(info, dict) else None
    if not isinstance(last, dict):
        return None         # a rate-limit-only update: no model call behind it
    total = info.get("total_token_usage")
    return {
        "session_total_tokens": _int(total.get("total_tokens")
                                     if isinstance(total, dict) else None),
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


def _subagent_started(p: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """multi_agent_v2 says which call started which thread; the call's own
    output only names the new agent by path."""
    if p.get("kind") != "started":
        return None
    return {"call_id": _text(p.get("event_id")),
            "agent_id": _text(p.get("agent_thread_id"))}


_EVENTS: Dict[str, Tuple[str, Callable[[Dict[str, Any]], Optional[Dict[str, Any]]]]] = {
    "task_started": (TURN_START, lambda p: {"turn_id": _text(p.get("turn_id"))}),
    "task_complete": (TURN_END, _turn_end),
    "turn_aborted": (TURN_END, _turn_end),
    "user_message": (USER_MESSAGE, lambda p: {"text": _text(p.get("message"))}),
    "agent_message": (AGENT_MESSAGE, lambda p: {"text": _text(p.get("message"))}),
    "token_count": (USAGE, _usage),
    "mcp_tool_call_end": (MCP_RESULT, _mcp_result),
    "sub_agent_activity": (SUBAGENT_STARTED, _subagent_started),
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
    if isinstance(output, list):
        output = _joined_text(output)
    if not isinstance(output, str):
        output = json.dumps(output)
    return {"call_id": _text(p.get("call_id")), "output": output}


def _joined_text(parts: List[Any]) -> Any:
    """Code mode's `exec` answers with content parts. Their text, one part
    per line: dumped as JSON, every newline would become `\\n`, and run
    together, `text(a); text(b)` would glue a's last line to b's first,
    so the scrubber would no longer see a `key = secret` line as one."""
    texts = [p.get("text") for p in parts
             if isinstance(p, dict) and isinstance(p.get("text"), str)]
    if not texts:
        return parts
    lines = [text if text.endswith("\n") else text + "\n" for text in texts[:-1]]
    return "".join(lines + texts[-1:])


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
    kind, parse = table.get(_text(payload.get("type")), (None, None))
    if kind is None:
        return None, None
    return kind, parse(payload)


def parse_line(line: str) -> Optional[Record]:
    """A Record, or None for a line that is not one we use or cannot read."""
    try:
        raw = json.loads(line)
    except (ValueError, RecursionError):
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


# A session record carries the base instructions (about 20 KB in 0.144.1).
_SESSION_LINE_MAX = 1 << 20


def read_session(path: str) -> Optional[Record]:
    """The session record a rollout opens with, or None."""
    try:
        with open(path, "rb") as fh:
            first = fh.readline(_SESSION_LINE_MAX)
    except OSError:
        return None
    record = parse_line(first.decode("utf-8", errors="replace"))
    return record if record is not None and record.kind == SESSION else None


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
            record.path = path
            records.append(record)
    return records, offset + start


def read_at(path: str, offset: int) -> Optional[Record]:
    """The record whose line starts at `offset`, or None."""
    try:
        with open(path, "rb") as fh:
            fh.seek(offset)
            line = fh.readline()
    except OSError:
        return None
    return parse_line(line.decode("utf-8", errors="replace"))
