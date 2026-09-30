"""What each model call's prompt was made of, as sizes: `rius.context.sizes`.

The console's Context panel splits a call's prompt into buckets: user and
assistant history, the current turn, and each tool's calls and results. It
needs either the prompt's content or this attribute, and a Claude Code
session sent neither, so the panel could only show the provider's cache
split. The Rius SDKs send the same attribute; the reader is argus-core
apps/sink/internal/attribution/sizes.go, schema version 1.

Only sizes and tool names are kept, so the running account can live in the
state file and the attribute is sent with content capture off. It is an
estimate from the transcript: Claude Code's system prompt, its tool
definitions and the reminders it injects are not in the transcript, and the
sink files whatever the sizes do not cover under `unattributed`.

The account is kept the way the sink reads it. Everything up to the last
assistant message is folded into per-role and per-tool byte sums; the
messages after it -- the prompt that opened the turn, or the tool results
the model is answering -- are listed in detail, so the sink can tell the
current turn from history.

Standard library only.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

VERSION = 1

# Detail kept for the messages after the last assistant message; more than
# this (a very wide parallel tool fan-out) folds the oldest. Bounds both the
# state file and the attribute, which the SDKs keep under 8 KB.
MAX_DETAIL = 50

# Named tools in the folded sums; the rest are summed under "" (the sink's
# tool:_other). The sink itself keeps only its 15 largest.
MAX_TOOLS = 30


def new() -> Dict[str, Any]:
    return {"messages": 0, "user_bytes": 0, "assistant_bytes": 0,
            "tools": {}, "detail": []}


def _nbytes(value: str) -> int:
    return len(value.encode("utf-8"))


def _account(scope: dict) -> Dict[str, Any]:
    acc = scope.get("context")
    if not isinstance(acc, dict):
        acc = new()
        scope["context"] = acc
    return acc


def _fold(acc: Dict[str, Any], message: Dict[str, Any]) -> None:
    acc["messages"] += 1
    for part in message.get("parts", []):
        if part.get("type") == "text":
            key = "assistant_bytes" if message.get("role") == "assistant" else "user_bytes"
            acc[key] += part.get("bytes", 0)
        else:
            tool = part.get("tool") or ""
            acc["tools"][tool] = acc["tools"].get(tool, 0) + part.get("bytes", 0)


def _add_detail(acc: Dict[str, Any], message: Dict[str, Any]) -> None:
    acc["detail"].append(message)
    while len(acc["detail"]) > MAX_DETAIL:
        _fold(acc, acc["detail"].pop(0))


def user_text(scope: dict, entry: Any) -> None:
    """A user message: a prompt, or text the harness injected as one."""
    if entry.raw.get("isCompactSummary"):
        # After a compaction the model sees the summary, not what it replaced.
        scope["context"] = new()
    text = entry.text()
    if text:
        _add_detail(_account(scope), {"role": "user", "parts": [
            {"type": "text", "bytes": _nbytes(text)}]})


def tool_results(scope: dict, parts: List[Dict[str, Any]]) -> None:
    """One user entry's tool results, as (tool name, content) sizes."""
    if parts:
        _add_detail(_account(scope), {"role": "user", "parts": parts})


def tool_result_part(tool_name: str, content: str) -> Dict[str, Any]:
    return {"type": "tool_call_response", "tool": tool_name,
            "bytes": _nbytes(content)}


def assistant_line(scope: dict, entry: Any, first_line: bool) -> None:
    """One transcript line of an assistant response.

    Whatever was in detail now precedes an assistant message, so it becomes
    history. Thinking is not counted: the API drops earlier turns' thinking
    from the prompt, and the sink reconciles what is left over.
    """
    acc = _account(scope)
    for message in acc["detail"]:
        _fold(acc, message)
    acc["detail"] = []
    if first_line:
        acc["messages"] += 1
    text = entry.text()
    if text:
        acc["assistant_bytes"] += _nbytes(text)
    for block in entry.tool_uses():
        name = block.get("name") or ""
        size = _nbytes(json.dumps(block.get("input") or {}))
        acc["tools"][name] = acc["tools"].get(name, 0) + size


def snapshot(scope: dict) -> str:
    """The attribute value for a call made now, before its response."""
    acc = _account(scope)
    value: Dict[str, Any] = {"version": VERSION,
                             "input_messages": list(acc["detail"])}
    if acc["messages"]:
        named = sorted(((n, b) for n, b in acc["tools"].items() if n),
                       key=lambda kv: (-kv[1], kv[0]))
        tools = [{"tool": name, "bytes": size} for name, size in named[:MAX_TOOLS]]
        rest = acc["tools"].get("", 0) + sum(b for _, b in named[MAX_TOOLS:])
        if rest:
            tools.append({"tool": "", "bytes": rest})
        value["folded"] = {
            "messages": acc["messages"],
            "user_bytes": acc["user_bytes"],
            "assistant_bytes": acc["assistant_bytes"],
            "tools": tools,
        }
    return json.dumps(value, separators=(",", ":"))
