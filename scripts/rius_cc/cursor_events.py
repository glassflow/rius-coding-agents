"""Cursor hook payload -> one timestamped line in a per-conversation spool.

Cursor's transcripts hold text only, so its hooks are the whole record: each
hook appends what it was told, stamped with the time it fired, and
cursor_spans.py builds the trace from the spool later.

The spool is a plaintext file that lives as long as the conversation, so it
gets the state file's rule: with capture off, no content is ever written to
it. Fields are kept by allowlist. An unknown field Cursor adds later is
dropped until someone decides which list it belongs on.

Standard library only. Nothing here writes to stdout: Cursor reads a hook's
stdout as its answer (permission, followup_message, ...), and stray output
there could block or steer the agent. The caller prints response_for().
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from typing import Any, Dict, List, Optional

from .spans import truncate

SPOOL_SUFFIX = ".jsonl"

# Ids, names, counts, flags and timings: what the trace's shape is made of.
IDENTITY_FIELDS = (
    "conversation_id", "generation_id", "session_id", "model", "model_id",
    "cursor_version", "cwd", "git_branch",
    "is_background_agent", "composer_mode",
    "reason", "final_status", "duration_ms", "duration",
    "tool_name", "tool_use_id", "failure_type", "is_interrupt",
    "mcp_server_name", "sandbox",
    "subagent_id", "subagent_type", "parent_conversation_id", "tool_call_id",
    "subagent_model", "is_parallel_worker",
    "status", "loop_count", "message_count", "tool_call_count",
    "trigger", "context_usage_percent", "context_tokens",
    "context_window_size", "messages_to_compact", "is_first_compaction",
)

# What the user typed, the model wrote, and the tools read and returned.
CONTENT_FIELDS = (
    "prompt", "attachments", "text", "agent_message",
    "tool_input", "tool_output", "error_message",
    "command", "output", "result_json", "file_path", "edits",
    "task", "description", "summary", "modified_files",
)

# Never kept: user_email (who, not what happened), transcript paths (the
# spool replaces the transcript), model_params.

_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def response_for(event: str) -> str:
    """What a Rius hook prints on stdout: an empty answer for every event.

    Cursor treats an absent `permission` as no objection, an absent
    `continue` as continue and an absent `followup_message` as no followup.
    "{}" rather than nothing, because a hook configured `failClosed` denies
    on empty output.
    """
    return "{}"


def spool_key(payload: Dict[str, Any]) -> str:
    """The conversation whose spool an event belongs in.

    A subagent's start and stop belong to the conversation that spawned it,
    which is where its span hangs.
    """
    return str(payload.get("parent_conversation_id")
               or payload.get("conversation_id") or "")


def spool_path(spool_dir: str, conversation_id: str) -> str:
    """One file per conversation. An id that is not a plain token is hashed,
    so a payload can never name a path outside the spool directory."""
    name = conversation_id
    if not _SAFE_NAME.match(name) or name.startswith("."):
        name = hashlib.sha256(name.encode("utf-8")).hexdigest()
    return os.path.join(spool_dir, name + SPOOL_SUFFIX)


def _content_value(value: Any, max_bytes: int) -> Any:
    if isinstance(value, str):
        return truncate(value, max_bytes)
    return truncate(json.dumps(value), max_bytes)


def shell_exit_code(tool_output: Any) -> Optional[int]:
    """The exit code in a Shell tool's output, `{"output", "exitCode"}`,
    which Cursor may hand over as an object or as its JSON string."""
    if isinstance(tool_output, str):
        try:
            tool_output = json.loads(tool_output)
        except ValueError:
            return None
    if not isinstance(tool_output, dict):
        return None
    code = tool_output.get("exitCode", tool_output.get("exit_code"))
    if isinstance(code, bool) or not isinstance(code, int):
        return None
    return code


def _cwd(payload: Dict[str, Any]) -> str:
    roots = payload.get("workspace_roots")
    if isinstance(roots, list) and roots and isinstance(roots[0], str):
        return roots[0]
    return ""


def to_record(payload: Dict[str, Any], now_ns: int, capture_content: bool,
              max_attr_bytes: int) -> Dict[str, Any]:
    """The spool line for one payload: allowlisted fields only, plus the
    time it fired and anything derived from content that is not content."""
    record: Dict[str, Any] = {"ts": now_ns,
                              "event": str(payload.get("hook_event_name") or "")}
    for key in IDENTITY_FIELDS:
        if key in payload:
            record[key] = payload[key]
    if "cwd" not in record and _cwd(payload):
        record["cwd"] = _cwd(payload)
    code = shell_exit_code(payload.get("tool_output"))
    if code is not None:
        # A number, not output: kept with capture off so a failed command
        # is still a failed span.
        record["exit_code"] = code
    if capture_content:
        for key in CONTENT_FIELDS:
            if key in payload and payload[key] is not None:
                record[key] = _content_value(payload[key], max_attr_bytes)
    return record


def _append_private(path: str, line: str) -> None:
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        if hasattr(os, "fchmod"):
            # O_CREAT's mode applies only to a new file.
            os.fchmod(fd, 0o600)
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)


def record(payload: Any, spool_dir: str, capture_content: bool,
           max_attr_bytes: int, clock=time.time_ns) -> Optional[str]:
    """Append one hook payload to its conversation's spool.

    Returns the spool path, or None when the payload names no conversation
    or the write failed. Never raises: a hook must not fail the agent.
    """
    if not isinstance(payload, dict):
        return None
    key = spool_key(payload)
    if not key:
        return None
    line = json.dumps(to_record(payload, clock(), capture_content,
                                max_attr_bytes)) + "\n"
    path = spool_path(spool_dir, key)
    try:
        _append_private(path, line)
    except OSError:
        return None
    return path


def read_spool(path: str) -> List[Dict[str, Any]]:
    """Every whole line of one spool. A torn last line (a hook killed
    mid-write) or a corrupt one is skipped."""
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().split("\n")
    except (OSError, UnicodeDecodeError):
        return []
    out = []
    for line in lines:
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict) and isinstance(item.get("ts"), int):
            out.append(item)
    return out


MAX_SUBAGENT_DEPTH = 5


def read_conversation(spool_dir: str, conversation_id: str) -> List[Dict[str, Any]]:
    """A conversation's events plus those its subagents spooled under their
    own ids, in the order they fired."""
    events: List[Dict[str, Any]] = []
    seen = set()
    pending = [(conversation_id, 0)]
    while pending:
        cid, depth = pending.pop()
        if cid in seen or depth > MAX_SUBAGENT_DEPTH:
            continue
        seen.add(cid)
        own = read_spool(spool_path(spool_dir, cid))
        events += own
        pending += [(str(e["subagent_id"]), depth + 1) for e in own
                    if e.get("event") == "subagentStart" and e.get("subagent_id")]
    events.sort(key=lambda e: e["ts"])
    return events
