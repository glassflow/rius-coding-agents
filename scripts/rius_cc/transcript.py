"""Claude Code transcript JSONL -> Entry objects.

Knows nothing about spans. Unknown line types are skipped silently: Claude Code
adds them without notice, and an unrecognised line must never be an error.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

CONVERSATIONAL = ("user", "assistant")


def _timestamp_ns(value: str) -> int:
    """RFC3339 -> integer Unix nanoseconds.

    Parsed by hand rather than via datetime.timestamp(), which returns a float
    and loses nanosecond precision for modern epochs.

    Claude Code emits a Z suffix today, but a numeric offset is equally legal
    RFC3339 and costs nothing to accept. It used to cost everything: with
    "+00:00", int("00+00") raised, parse_line swallowed it, and every single
    line was dropped forever with zero spans and zero errors.
    """
    import calendar

    date_part, _, time_part = value.partition("T")

    offset_s = 0
    if time_part[-1:] in ("Z", "z"):
        time_part = time_part[:-1]
    else:
        # The date has already been split off, so a '+' or '-' left in the
        # time part can only be the UTC offset.
        idx = max(time_part.rfind("+"), time_part.rfind("-"))
        if idx > 0:
            sign = -1 if time_part[idx] == "-" else 1
            off_h, _, off_m = time_part[idx + 1:].partition(":")
            offset_s = sign * (int(off_h) * 3600 + int(off_m or 0) * 60)
            time_part = time_part[:idx]

    hms, _, frac = time_part.partition(".")
    year, month, day = (int(x) for x in date_part.split("-"))
    hour, minute, second = (int(x) for x in hms.split(":"))
    epoch = calendar.timegm((year, month, day, hour, minute, second, 0, 0, 0))
    epoch -= offset_s
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


def parse_line_ex(line: str) -> Tuple[Optional[Entry], Optional[str]]:
    """(entry, skip_reason).

    skip_reason is None both for a parsed entry and for a line that is
    SUPPOSED to be ignored -- blank lines and the many non-conversational
    record types Claude Code writes. It is a string only when a line that
    looked like ours could not be read, which is the thing worth counting:
    a transcript nothing can parse is indistinguishable from an idle session.
    """
    line = line.strip()
    if not line:
        return None, None
    try:
        raw = json.loads(line)
    except ValueError:
        return None, "not valid JSON"
    if not isinstance(raw, dict):
        return None, "not a JSON object"
    if raw.get("type") not in CONVERSATIONAL:
        return None, None          # normal: summary, mode, file-history, ...
    if "timestamp" not in raw or "uuid" not in raw:
        return None, "conversational line with no timestamp or uuid"
    try:
        return Entry(raw), None
    except (ValueError, KeyError, TypeError) as exc:
        return None, "could not read the entry (%s)" % type(exc).__name__


def parse_line(line: str) -> Optional[Entry]:
    return parse_line_ex(line)[0]


def read_from(path: str, offset: int, stats: Optional[dict] = None
              ) -> Tuple[List[Entry], int]:
    """Read complete lines from `offset`. Returns (entries, new_offset).

    A trailing partial line is left unconsumed -- the exporter races the writer,
    and half a JSON object must not advance the offset past itself.

    `stats`, if given, is filled in with "skipped" (how many lines looked like
    ours but could not be read) and "first_skipped_reason". The caller is
    expected to persist and log those: silently dropping every line looks
    exactly like a session where nothing happened.
    """
    skipped = 0
    first_reason = None

    def finish(entries, consumed):
        if stats is not None:
            stats["skipped"] = skipped
            stats["first_skipped_reason"] = first_reason
        return entries, consumed

    try:
        size = __import__("os").path.getsize(path)
    except OSError:
        return finish([], offset)
    if offset > size:
        offset = 0          # transcript was compacted or replaced; re-derive
    entries: List[Entry] = []
    consumed = offset
    try:
        with open(path, "rb") as fh:
            fh.seek(offset)
            buf = fh.read()
    except OSError:
        return finish([], offset)
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
            skipped += 1
            if first_reason is None:
                first_reason = "not valid UTF-8"
            continue
        entry, reason = parse_line_ex(text)
        if entry is not None:
            entries.append(entry)
        elif reason is not None:
            skipped += 1
            if first_reason is None:
                first_reason = reason
    return finish(entries, consumed)
