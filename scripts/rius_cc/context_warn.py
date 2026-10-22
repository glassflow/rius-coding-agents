"""Tell the user when a Claude Code session's context is filling up.

Two short lines, both shown through hook JSON `systemMessage` (the user sees
it; the model never does, and nothing here is ever printed as plain stdout):

  * at Stop, once when the context first passes 70% of the model's window and
    once more when it first passes 90%;
  * at SessionStart after a compaction (and at Stop, for a compaction the
    SessionStart did not show), how much the compaction dropped.

Everything is read locally from the tail of the session's transcript. No
network, and no tracing needed: it works in a folder where tracing is off.
It does need a stored key, because scripts/hook.sh exits before starting
Python on a machine without one (RIUS-1224): a user who never signed in pays
for no hook, and is not warned.

The context size is the last real assistant row's `usage`: input plus cache
creation plus cache read tokens. Rows that are not a real model call
(`<synthetic>`, API errors, subagent rows) are skipped, and so is a row from
before the latest compaction, which no longer describes the context.

The window is the hard part. A transcript records the plain model id whether
the session runs at the standard window or at 1M, and a 1M session on these
models is common, so a guess of 200k would warn at 140k in a session that
has 860k left. What the transcripts say (see WINDOWS) is used as far as it
goes: an explicit `[1m]` on the requested model, or a context or compaction
size above the standard window, proves the larger one. Until then the
standard window is assumed. A model with no entry in the table gets no
warning at all rather than a guess.

What a session was shown is kept in a small file beside the notice records
(`notices/<session>.context`): the level reached, the compaction line shown
last, and whether the session proved to have the larger window. Sizes and
ids only, nothing from the transcript's content.

Standard library only.
"""
from __future__ import annotations

import json
import os
import tempfile
from typing import Any, Dict, Iterator, List, Optional

from . import agent, log, platform_compat, state, transcript

OFF_VALUES = ("0", "false", "off")

THRESHOLDS = (70, 90)

STANDARD_WINDOW = 200_000
LARGE_WINDOW = 1_000_000

# Model-id prefix -> (standard window, larger window). A model matches a
# prefix when it equals it or continues it with "-" (dated ids). Entries are
# the models whose transcripts here prove both windows: they run compactions
# at about 167k (a standard window less Claude Code's reserve), carry the
# `[1m]` marker on `requestedModel` for explicitly selected 1M sessions, and
# have recorded contexts of up to 967k with no marker. The Claude Code
# models without such evidence (sonnet-4-6, haiku) are left out on purpose.
WINDOWS = {
    "claude-opus-5": (STANDARD_WINDOW, LARGE_WINDOW),
    "claude-sonnet-5": (STANDARD_WINDOW, LARGE_WINDOW),
    "claude-fable-5": (STANDARD_WINDOW, LARGE_WINDOW),
}

LARGE_MARKER = "[1m]"

# The tail is read backwards in chunks until the rows it needs are found, and
# never further than the cap: a transcript is megabytes of tool output.
CHUNK_BYTES = 64 * 1024
MAX_SCAN_BYTES = 2 * 1024 * 1024

WARNING = ("Context is %d%% full (%s of %s). Quality may drop; consider "
           "/compact or a new session.")
COMPACTED = ("Compacted %s to %s tokens. Check that instructions you rely on "
             "are still followed.")
COMPACTED_FROM = "Compacted from %s tokens."


def enabled(env) -> bool:
    """RIUS_CONTEXT_WARN=0 (or false, off) turns both lines off."""
    return env.get("RIUS_CONTEXT_WARN", "").strip().lower() not in OFF_VALUES


def tokens(count: int) -> str:
    """144_000 -> "144k", 1_000_000 -> "1M": to the nearest thousand."""
    if count >= LARGE_WINDOW and count % LARGE_WINDOW == 0:
        return "%dM" % (count // LARGE_WINDOW)
    if count < 1000:
        return str(count)
    return "%dk" % ((count + 500) // 1000)


def _count(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def windows_for(model: Any) -> Optional[tuple]:
    if not isinstance(model, str):
        return None
    for prefix, windows in WINDOWS.items():
        if model == prefix or model.startswith(prefix + "-"):
            return windows
    return None


def window_for(model: Any, requested: Any, context: int,
               proven_large: bool) -> Optional[int]:
    """The window to measure `context` against, or None for a model that is
    not in the table."""
    windows = windows_for(model)
    if windows is None:
        return None
    standard, large = windows
    marked = isinstance(requested, str) and LARGE_MARKER in requested.lower()
    if marked or proven_large or context > standard:
        return large
    return standard


class Boundary:
    """A compaction, as the transcript's compact_boundary row says it."""
    __slots__ = ("id", "before", "after")

    def __init__(self, entry) -> None:
        meta = entry.compact_metadata()
        self.id = entry.uuid or str(entry.timestamp_ns)
        self.before = _count(meta.get("preTokens"))
        self.after = _count(meta.get("postTokens"))

    def line(self) -> Optional[str]:
        if not self.before:
            return None
        if not self.after:
            return COMPACTED_FROM % tokens(self.before)
        return COMPACTED % (tokens(self.before), tokens(self.after))


class Tail:
    """What the end of a transcript says: the context of the last real model
    call since the latest compaction, and that compaction."""
    __slots__ = ("context", "model", "requested", "boundary")

    def __init__(self) -> None:
        self.context: Optional[int] = None
        self.model: Any = None
        self.requested: Any = None
        self.boundary: Optional[Boundary] = None


def _lines_from_end(path: str) -> Iterator[bytes]:
    """The file's lines, last first, reading at most MAX_SCAN_BYTES of it. A
    line cut by the cap is not yielded."""
    with open(path, "rb") as fh:
        pos = fh.seek(0, os.SEEK_END)
        floor = max(0, pos - MAX_SCAN_BYTES)
        buf = b""
        while pos > floor:
            size = min(CHUNK_BYTES, pos - floor)
            pos -= size
            fh.seek(pos)
            buf = fh.read(size) + buf
            parts = buf.split(b"\n")
            buf = parts[0]
            for line in reversed(parts[1:]):
                yield line
        if pos == 0:
            yield buf


def _context_of(entry) -> Optional[int]:
    """The context a real assistant row was sent with, or None."""
    if entry.kind != "assistant" or entry.is_sidechain:
        return None
    if entry.raw.get("isApiErrorMessage") or entry.message.get("model") in (
            None, "<synthetic>"):
        return None
    usage = entry.message.get("usage")
    if not isinstance(usage, dict):
        return None
    total = sum(_count(usage.get(key)) or 0 for key in (
        "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    return total or None


def read_tail(path: str) -> Tail:
    """Walk the transcript backwards for its last real assistant row and its
    last compaction. Stops at the compaction: a row from before it is stale.
    Lines that are cut off or not JSON are skipped."""
    tail = Tail()
    for raw in _lines_from_end(path):
        if b'"compact_boundary"' in raw:
            entry = _parse(raw)
            if entry is not None and entry.is_compact_boundary():
                tail.boundary = Boundary(entry)
                break
        if (tail.context is None and b'"usage"' in raw
                and b'"assistant"' in raw):
            entry = _parse(raw)
            context = _context_of(entry) if entry is not None else None
            if context is not None:
                tail.context = context
                tail.model = entry.message.get("model")
                tail.requested = entry.raw.get("requestedModel")
    return tail


def _parse(raw: bytes):
    try:
        return transcript.parse_line(raw.decode("utf-8"))
    except UnicodeDecodeError:
        return None


def threshold_line(context: int, window: int) -> Optional[tuple]:
    """(level, line) for the highest threshold `context` has passed, or None."""
    for level in sorted(THRESHOLDS, reverse=True):
        if context * 100 >= window * level:
            return level, WARNING % (context * 100 // window, tokens(context),
                                     tokens(window))
    return None


def _record_path(session_id: str, home: str) -> str:
    return os.path.join(agent.active().rius_dir(home), "notices",
                        state.require_valid_session_id(session_id) + ".context")


def _load(session_id: str, home: str) -> Dict[str, Any]:
    """What this session was shown, or what the session it continues was."""
    ids = [session_id]
    predecessor = state.load(session_id, home).get("continued_from")
    if isinstance(predecessor, str) and state.is_valid_session_id(predecessor):
        ids.append(predecessor)
    for sid in ids:
        try:
            with open(_record_path(sid, home)) as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            return {"level": _count(data.get("level")) or 0,
                    "boundary": data.get("boundary") if isinstance(
                        data.get("boundary"), str) else "",
                    "large": data.get("large") is True}
    return _load_none()


def _load_none() -> Dict[str, Any]:
    return {"level": 0, "boundary": "", "large": False}


def _save(session_id: str, home: str, record: Dict[str, Any]) -> None:
    path = _record_path(session_id, home)
    directory = platform_compat.ensure_private_dir(os.path.dirname(path))
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".rius-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(record, fh)
        platform_compat.replace_atomic(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def lines(event: str, payload: dict, session_id: str, home: str) -> List[str]:
    """The lines to show for this hook event; [] for most events. Raises on
    trouble: `messages` is the entry that never does."""
    if event == "SessionStart" and payload.get("source") != "compact":
        return []
    path = payload.get("transcript_path")
    if event not in ("SessionStart", "Stop") or not isinstance(path, str) \
            or not path:
        return []
    tail = read_tail(path)
    record = _load(session_id, home)
    before = dict(record)
    out: List[str] = []
    if tail.boundary is not None:
        if tail.boundary.before and tail.boundary.before > STANDARD_WINDOW:
            record["large"] = True
        if tail.boundary.id != record["boundary"]:
            record["boundary"] = tail.boundary.id
            record["level"] = 0         # the usage fell: the warnings re-arm
            line = tail.boundary.line()
            if line:
                out.append(line)
    if event == "Stop" and tail.context is not None:
        if tail.context > STANDARD_WINDOW and windows_for(tail.model):
            record["large"] = True
        window = window_for(tail.model, tail.requested, tail.context,
                            record["large"])
        passed = threshold_line(tail.context, window) if window else None
        level = passed[0] if passed is not None else 0
        if level > record["level"]:
            out.append(passed[1])
        # Lower too: a context under the level shown means it was emptied
        # (a compaction the scan did not reach, a /clear), so it warns anew.
        if window:
            record["level"] = level
    if record != before or (record != _load_none() and not os.path.exists(
            _record_path(session_id, home))):
        # Also the first write of a record taken over from the predecessor.
        _save(session_id, home, record)
    return out


def messages(event: str, payload: dict, session_id: str, cfg, home: str,
             env=None) -> List[str]:
    """`lines`, never raising: this runs in the session's critical path."""
    if not enabled(os.environ if env is None else env):
        return []
    try:
        return lines(event, payload, session_id, home)
    except Exception as exc:
        log.write(home, cfg, "session %s: context warning failed (%s)"
                  % (session_id, type(exc).__name__), force=True)
        return []
