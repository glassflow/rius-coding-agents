"""Claude Code transcript JSONL -> Entry objects.

Knows nothing about spans. Unknown line types are skipped silently: Claude Code
adds them without notice, and an unrecognised line must never be an error.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

CONVERSATIONAL = ("user", "assistant")


def _timestamp_ns(value: str) -> int:
    """RFC3339 UTC (always Z-suffixed here) -> integer Unix nanoseconds.

    Parsed by hand rather than via datetime.timestamp(), which returns a float
    and loses nanosecond precision for modern epochs.
    """
    import calendar

    date_part, _, time_part = value.partition("T")
    time_part = time_part.rstrip("Z")
    hms, _, frac = time_part.partition(".")
    year, month, day = (int(x) for x in date_part.split("-"))
    hour, minute, second = (int(x) for x in hms.split(":"))
    epoch = calendar.timegm((year, month, day, hour, minute, second, 0, 0, 0))
    nanos = int((frac + "000000000")[:9]) if frac else 0
    return epoch * 1_000_000_000 + nanos


class Entry:
    __slots__ = (
        "uuid", "parent_uuid", "kind", "timestamp_ns", "session_id",
        "is_sidechain", "cwd", "git_branch", "cc_version", "prompt_id",
        "message", "raw",
    )

    def __init__(self, raw: Dict[str, Any]) -> None:
        self.raw = raw
        self.uuid = raw.get("uuid") or ""
        self.parent_uuid = raw.get("parentUuid")
        self.kind = raw.get("type") or ""
        self.timestamp_ns = _timestamp_ns(raw["timestamp"])
        self.session_id = raw.get("sessionId") or ""
        self.is_sidechain = bool(raw.get("isSidechain"))
        self.cwd = raw.get("cwd") or ""
        self.git_branch = raw.get("gitBranch") or ""
        self.cc_version = raw.get("version") or ""
        self.prompt_id = raw.get("promptId")
        self.message = raw.get("message") or {}

    def _blocks(self, block_type: str) -> List[Dict[str, Any]]:
        content = self.message.get("content")
        if not isinstance(content, list):
            return []
        return [b for b in content
                if isinstance(b, dict) and b.get("type") == block_type]

    def tool_uses(self) -> List[Dict[str, Any]]:
        return self._blocks("tool_use")

    def tool_results(self) -> List[Dict[str, Any]]:
        return self._blocks("tool_result")

    def text(self) -> str:
        content = self.message.get("content")
        if isinstance(content, str):
            return content
        return "".join(b.get("text", "") for b in self._blocks("text"))


def parse_line(line: str) -> Optional[Entry]:
    line = line.strip()
    if not line:
        return None
    try:
        raw = json.loads(line)
    except ValueError:
        return None
    if not isinstance(raw, dict):
        return None
    if raw.get("type") not in CONVERSATIONAL:
        return None
    if "timestamp" not in raw or "uuid" not in raw:
        return None
    try:
        return Entry(raw)
    except (ValueError, KeyError, TypeError):
        return None


def read_from(path: str, offset: int) -> Tuple[List[Entry], int]:
    """Read complete lines from `offset`. Returns (entries, new_offset).

    A trailing partial line is left unconsumed -- the exporter races the writer,
    and half a JSON object must not advance the offset past itself.
    """
    try:
        size = __import__("os").path.getsize(path)
    except OSError:
        return [], offset
    if offset > size:
        offset = 0          # transcript was compacted or replaced; re-derive
    entries: List[Entry] = []
    consumed = offset
    try:
        with open(path, "rb") as fh:
            fh.seek(offset)
            buf = fh.read()
    except OSError:
        return [], offset
    start = 0
    while True:
        nl = buf.find(b"\n", start)
        if nl == -1:
            break
        raw_line = buf[start:nl]
        start = nl + 1
        consumed = offset + start
        try:
            text = raw_line.decode("utf-8")
        except UnicodeDecodeError:
            continue
        entry = parse_line(text)
        if entry is not None:
            entries.append(entry)
    return entries, consumed
