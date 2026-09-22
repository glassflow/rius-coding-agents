# rius-claude-code Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Claude Code plugin that streams the running session to Rius as OTLP GenAI traces, live, opt-in per folder.

**Architecture:** The session transcript JSONL is the only data source; hooks supply timing and control. Span IDs are derived by hashing the session id and each entry's `uuid`, so no identity is ever carried between processes and any replay is idempotent. Each hook spawns a short-lived detached exporter that reads new transcript lines from a saved byte offset, builds spans, encodes OTLP protobuf by hand, and POSTs them. In-flight spans use Rius's `glassflow.span.pending` mechanism.

**Tech Stack:** Python 3 standard library only at runtime (`json`, `hashlib`, `struct`, `urllib.request`, `fcntl`, `os`, `subprocess`). `pytest` and `opentelemetry-proto==1.43.0` are test-only.

**Spec:** `docs/superpowers/specs/2026-09-22-rius-claude-code-design.md` — read it before Task 1. The plan argues from the spec; where they disagree, the spec wins and the plan is wrong.

## Global Constraints

These apply to every task. Do not restate them per task; do not violate them in any task.

- **Zero runtime dependencies.** Shipped code under `scripts/` imports only the Python standard library. No `glassflow-rius`, no OpenTelemetry SDK, no `protobuf`, no `requests`. Test code may import `pytest` and `opentelemetry-proto`.
- **Python 3.9+.** No `match` statements, no PEP 604 `X | Y` in runtime annotations unless `from __future__ import annotations` is present at the top of the file.
- **A hook must never fail a session.** Every process entry point wraps its body in `try/except BaseException`, logs, and exits 0. There is no code path that exits non-zero.
- **Instrumentation scope name is the literal `glassflow`.** Not `rius`, not `rius-claude-code`. It is wire-visible and the backend keys on it.
- **Never log, print, or write an API key**, including in debug output and error messages. Redact to `glassflow_…`.
- **Content attribute values are capped** at `RIUS_CLAUDE_MAX_ATTR_BYTES` (default 32768) with a visible ` …[truncated N bytes]` suffix.
- **Timestamps are integer Unix nanoseconds.** Never floats — float seconds lose nanosecond precision above ~2^53.
- **Commit after every task**, with the task name in the message.

## File Structure

```
.claude-plugin/plugin.json          plugin manifest
.claude-plugin/marketplace.json     marketplace serving this one plugin (needed to install at all)
hooks/hooks.json                    six hook events -> scripts/hook.py
scripts/hook.py                     CRITICAL PATH: resolve config, spawn exporter, exit 0
scripts/exporter.py                 detached: lock, read, build, encode, POST, save
scripts/rius_cc/__init__.py
scripts/rius_cc/proto.py            protobuf wire primitives (Task 1)
scripts/rius_cc/transcript.py       JSONL -> Entry objects (Task 2)
scripts/rius_cc/spans.py            Entry objects -> Span tree, derived IDs, semconv (Task 3)
scripts/rius_cc/config.py           resolution ladder (Task 4)
scripts/rius_cc/state.py            offset + open tools + flock (Task 5)
scripts/rius_cc/otlp.py             Span tree -> OTLP bytes, HTTP export (Task 6)
commands/rius.md                    /rius slash command
tests/                              pytest suite
tests/fixtures/*.jsonl              sanitized transcripts
```

Each module is testable alone: `transcript.py` knows nothing of spans, `spans.py` nothing of HTTP, `proto.py` nothing of OTLP semantics, `otlp.py` nothing of Claude Code.

---

### Task 0: Repo scaffold

**Files:**
- Create: `pyproject.toml`, `scripts/rius_cc/__init__.py`, `tests/__init__.py`, `tests/conftest.py`

**Interfaces:**
- Consumes: nothing.
- Produces: an importable `rius_cc` package and a working `pytest` invocation for every later task.

- [ ] **Step 1: Create `pyproject.toml`**

```toml
[project]
name = "rius-claude-code"
version = "0.1.0"
description = "Stream Claude Code sessions to Rius as OTLP GenAI traces"
requires-python = ">=3.9"
dependencies = []

[project.optional-dependencies]
test = ["pytest>=7.0", "opentelemetry-proto==1.43.0"]

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["scripts"]
```

`dependencies = []` is load-bearing — it is the machine-readable statement of the zero-runtime-dependency constraint.

- [ ] **Step 2: Create the package and test markers**

```bash
mkdir -p scripts/rius_cc tests/fixtures
printf '"""Rius instrumentation for Claude Code."""\n' > scripts/rius_cc/__init__.py
touch tests/__init__.py
```

- [ ] **Step 3: Create `tests/conftest.py`**

```python
"""Shared fixtures. `pythonpath = ["scripts"]` in pyproject makes rius_cc importable."""
import pathlib

import pytest


@pytest.fixture
def fixtures_dir() -> pathlib.Path:
    return pathlib.Path(__file__).parent / "fixtures"
```

- [ ] **Step 4: Verify the toolchain**

Run: `python3 -m pytest -q`
Expected: `no tests ran` — and crucially, no collection error. A collection error here means `pythonpath` is wrong and every later task will fail confusingly.

- [ ] **Step 5: Install the test-only dependency**

Run: `python3 -m pip install --user 'pytest>=7.0' 'opentelemetry-proto==1.43.0'`
Expected: both install. This is never added to runtime `dependencies`.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml scripts tests
git commit -m "chore: scaffold package and pytest"
```

---

### Task 1: Protobuf wire primitives

**Files:**
- Create: `scripts/rius_cc/proto.py`
- Test: `tests/test_proto.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `varint(v: int) -> bytes`
  - `tag(field: int, wire: int) -> bytes`
  - `ld(field: int, payload: bytes) -> bytes` — length-delimited
  - `string_field(field: int, text: str) -> bytes`
  - `bytes_field(field: int, raw: bytes) -> bytes`
  - `varint_field(field: int, v: int) -> bytes`
  - `fixed64_field(field: int, v: int) -> bytes`
  - `double_field(field: int, v: float) -> bytes`

  Every one returns `b""` when the value equals the proto3 default, except `ld`, which always emits.

The field numbers and wire types below were extracted from `opentelemetry-proto==1.43.0` descriptors and verified byte-for-byte against the reference serializer. Do not adjust them from memory.

- [ ] **Step 1: Write the failing test**

```python
import struct

import pytest

from rius_cc import proto


def test_varint_single_byte():
    assert proto.varint(0) == b"\x00"
    assert proto.varint(1) == b"\x01"
    assert proto.varint(127) == b"\x7f"


def test_varint_multi_byte():
    assert proto.varint(128) == b"\x80\x01"
    assert proto.varint(300) == b"\xac\x02"


def test_tag_is_field_shifted_plus_wire_type():
    assert proto.tag(1, 2) == b"\x0a"      # TracesData.resource_spans
    assert proto.tag(7, 1) == b"\x39"      # Span.start_time_unix_nano, fixed64
    assert proto.tag(16, 5) == b"\x85\x01" # Span.flags, two-byte tag


def test_string_field_omits_proto3_default():
    assert proto.string_field(3, "") == b""
    assert proto.string_field(5, "ab") == b"\x2a\x02ab"


def test_varint_field_omits_zero():
    assert proto.varint_field(6, 0) == b""
    assert proto.varint_field(6, 1) == b"\x30\x01"


def test_fixed64_is_little_endian_eight_bytes():
    ns = 1758573545117000000
    assert proto.fixed64_field(7, ns) == b"\x39" + struct.pack("<Q", ns)


def test_fixed64_omits_zero():
    assert proto.fixed64_field(7, 0) == b""


def test_bytes_field_omits_empty():
    assert proto.bytes_field(4, b"") == b""
    assert proto.bytes_field(2, b"\x01" * 8) == b"\x12\x08" + b"\x01" * 8


def test_ld_always_emits_even_when_empty():
    # an empty submessage is presence, not a default
    assert proto.ld(15, b"") == b"\x7a\x00"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_proto.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'rius_cc.proto'`

- [ ] **Step 3: Write the implementation**

```python
"""Minimal protobuf wire-format encoder.

Encode-only, for a fixed schema (OTLP TracesData). No parsing, no unknown
fields, no schema evolution -- which is what keeps this small enough to own.

Proto3 default omission is deliberate: emitting a singular scalar equal to its
default is valid wire format, but omitting it is what makes our output
byte-identical to the reference serializer, and that equality is the property
tests/test_otlp_roundtrip.py checks.
"""
from __future__ import annotations

import struct

WIRE_VARINT = 0
WIRE_FIXED64 = 1
WIRE_LEN = 2
WIRE_FIXED32 = 5


def varint(value: int) -> bytes:
    """Base-128, least-significant group first, MSB set on continuation."""
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def tag(field: int, wire: int) -> bytes:
    return varint((field << 3) | wire)


def ld(field: int, payload: bytes) -> bytes:
    """Length-delimited. Always emits -- an empty submessage means presence."""
    return tag(field, WIRE_LEN) + varint(len(payload)) + payload


def string_field(field: int, text: str) -> bytes:
    if not text:
        return b""
    return ld(field, text.encode("utf-8"))


def bytes_field(field: int, raw: bytes) -> bytes:
    if not raw:
        return b""
    return ld(field, raw)


def varint_field(field: int, value: int) -> bytes:
    if not value:
        return b""
    # int64 negatives are two's complement as unsigned 64-bit (not zigzag --
    # zigzag is sint32/sint64 only, which appear nowhere in OTLP).
    if value < 0:
        value &= 0xFFFFFFFFFFFFFFFF
    return tag(field, WIRE_VARINT) + varint(value)


def fixed64_field(field: int, value: int) -> bytes:
    if not value:
        return b""
    return tag(field, WIRE_FIXED64) + struct.pack("<Q", value)


def double_field(field: int, value: float) -> bytes:
    if value == 0:
        return b""
    return tag(field, WIRE_FIXED64) + struct.pack("<d", value)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_proto.py -q`
Expected: 9 passed.

- [ ] **Step 5: Commit**

```bash
git add scripts/rius_cc/proto.py tests/test_proto.py
git commit -m "feat: protobuf wire primitives"
```

---

### Task 2: Transcript parsing

**Files:**
- Create: `scripts/rius_cc/transcript.py`
- Test: `tests/test_transcript.py`
- Create: `tests/fixtures/simple.jsonl`, `tests/fixtures/tool_call.jsonl`, `tests/fixtures/tool_error.jsonl`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `class Entry` with attributes: `uuid: str`, `parent_uuid: Optional[str]`, `kind: str` (`"user"`/`"assistant"`), `timestamp_ns: int`, `session_id: str`, `is_sidechain: bool`, `cwd: str`, `git_branch: str`, `cc_version: str`, `prompt_id: Optional[str]`, `message: dict`, `raw: dict`
  - `parse_line(line: str) -> Optional[Entry]` — `None` for blank, malformed, or non-conversational lines
  - `read_from(path: str, offset: int) -> Tuple[List[Entry], int]` — returns entries and the new byte offset, never consuming a trailing partial line
  - `Entry.tool_uses() -> List[dict]` — `tool_use` content blocks on an assistant entry
  - `Entry.tool_results() -> List[dict]` — `tool_result` content blocks on a user entry
  - `Entry.text() -> str` — concatenated `text` blocks

- [ ] **Step 1: Write the fixtures**

Fixtures are three-line files. Write them literally; they are the ground truth every later task tests against. Note `timestamp` is RFC3339 UTC with `Z`.

`tests/fixtures/simple.jsonl`:

```
{"type":"mode","mode":"normal","sessionId":"11111111-1111-1111-1111-111111111111"}
{"parentUuid":null,"isSidechain":false,"type":"user","uuid":"u1","timestamp":"2026-09-22T10:00:00.000Z","sessionId":"11111111-1111-1111-1111-111111111111","promptId":"p1","cwd":"/tmp/proj","gitBranch":"main","version":"2.1.278","message":{"role":"user","content":[{"type":"text","text":"hello"}]}}
{"parentUuid":"u1","isSidechain":false,"type":"assistant","uuid":"a1","timestamp":"2026-09-22T10:00:02.500Z","sessionId":"11111111-1111-1111-1111-111111111111","requestId":"req_1","cwd":"/tmp/proj","gitBranch":"main","version":"2.1.278","message":{"role":"assistant","model":"claude-opus-5","stop_reason":"end_turn","content":[{"type":"text","text":"hi there"}],"usage":{"input_tokens":10,"output_tokens":5,"cache_read_input_tokens":100,"cache_creation_input_tokens":200}}}
```

`tests/fixtures/tool_call.jsonl`:

```
{"parentUuid":null,"isSidechain":false,"type":"user","uuid":"u1","timestamp":"2026-09-22T10:00:00.000Z","sessionId":"22222222-2222-2222-2222-222222222222","promptId":"p1","cwd":"/tmp/proj","gitBranch":"main","version":"2.1.278","message":{"role":"user","content":[{"type":"text","text":"read a file"}]}}
{"parentUuid":"u1","isSidechain":false,"type":"assistant","uuid":"a1","timestamp":"2026-09-22T10:00:01.000Z","sessionId":"22222222-2222-2222-2222-222222222222","cwd":"/tmp/proj","gitBranch":"main","version":"2.1.278","message":{"role":"assistant","model":"claude-opus-5","stop_reason":"tool_use","content":[{"type":"tool_use","id":"toolu_1","name":"Read","input":{"file_path":"/tmp/x"}}],"usage":{"input_tokens":10,"output_tokens":5}}}
{"parentUuid":"a1","isSidechain":false,"type":"user","uuid":"u2","timestamp":"2026-09-22T10:00:03.000Z","sessionId":"22222222-2222-2222-2222-222222222222","promptId":"p1","sourceToolAssistantUUID":"a1","cwd":"/tmp/proj","gitBranch":"main","version":"2.1.278","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_1","is_error":false,"content":"file body"}]}}
```

`tests/fixtures/tool_error.jsonl` — identical to `tool_call.jsonl` but with `"is_error":true` and `"content":"ENOENT"` on the final line, and session id `33333333-3333-3333-3333-333333333333`.

- [ ] **Step 2: Write the failing test**

```python
from rius_cc import transcript


def test_skips_non_conversational_lines(fixtures_dir):
    entries, _ = transcript.read_from(str(fixtures_dir / "simple.jsonl"), 0)
    assert [e.kind for e in entries] == ["user", "assistant"]


def test_parses_entry_scalars(fixtures_dir):
    entries, _ = transcript.read_from(str(fixtures_dir / "simple.jsonl"), 0)
    user, asst = entries
    assert user.uuid == "u1"
    assert user.parent_uuid is None
    assert user.prompt_id == "p1"
    assert user.cwd == "/tmp/proj"
    assert user.git_branch == "main"
    assert user.cc_version == "2.1.278"
    assert asst.parent_uuid == "u1"
    assert asst.message["model"] == "claude-opus-5"


def test_timestamp_is_integer_nanoseconds(fixtures_dir):
    entries, _ = transcript.read_from(str(fixtures_dir / "simple.jsonl"), 0)
    assert entries[0].timestamp_ns == 1758535200000000000
    # .500Z must survive as exactly half a second, not a float artefact
    assert entries[1].timestamp_ns - entries[0].timestamp_ns == 2_500_000_000


def test_offset_advances_to_end_of_file(fixtures_dir, tmp_path):
    src = (fixtures_dir / "simple.jsonl").read_bytes()
    p = tmp_path / "t.jsonl"
    p.write_bytes(src)
    entries, offset = transcript.read_from(str(p), 0)
    assert offset == len(src)
    more, offset2 = transcript.read_from(str(p), offset)
    assert more == [] and offset2 == offset


def test_trailing_partial_line_is_not_consumed(tmp_path, fixtures_dir):
    src = (fixtures_dir / "simple.jsonl").read_bytes()
    partial = src + b'{"type":"user","uuid":"u9"'
    p = tmp_path / "t.jsonl"
    p.write_bytes(partial)
    entries, offset = transcript.read_from(str(p), 0)
    assert offset == len(src)          # stopped before the partial line
    assert all(e.uuid != "u9" for e in entries)


def test_malformed_line_is_skipped_not_raised(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_bytes(b'{"broken\n{"type":"mode"}\n')
    entries, offset = transcript.read_from(str(p), 0)
    assert entries == []
    assert offset == p.stat().st_size


def test_tool_use_and_result_blocks(fixtures_dir):
    entries, _ = transcript.read_from(str(fixtures_dir / "tool_call.jsonl"), 0)
    asst = entries[1]
    uses = asst.tool_uses()
    assert len(uses) == 1
    assert uses[0]["name"] == "Read"
    assert uses[0]["id"] == "toolu_1"
    results = entries[2].tool_results()
    assert results[0]["tool_use_id"] == "toolu_1"
    assert results[0]["is_error"] is False


def test_text_concatenates_text_blocks(fixtures_dir):
    entries, _ = transcript.read_from(str(fixtures_dir / "simple.jsonl"), 0)
    assert entries[1].text() == "hi there"
```

- [ ] **Step 3: Run test to verify it fails**

Run: `python3 -m pytest tests/test_transcript.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'rius_cc.transcript'`

- [ ] **Step 4: Write the implementation**

```python
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
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_transcript.py -q`
Expected: 8 passed.

If `test_timestamp_is_integer_nanoseconds` fails on the absolute value, recompute the expected constant with
`python3 -c "import calendar;print(calendar.timegm((2026,9,22,10,0,0,0,0,0))*10**9)"`
and correct the test — the relative assertion is the one that must not be weakened.

- [ ] **Step 6: Commit**

```bash
git add scripts/rius_cc/transcript.py tests/test_transcript.py tests/fixtures
git commit -m "feat: transcript JSONL parsing"
```

---

### Task 3: Span tree

**Files:**
- Create: `scripts/rius_cc/spans.py`
- Test: `tests/test_spans.py`

**Interfaces:**
- Consumes: `transcript.Entry` (Task 2).
- Produces:
  - `trace_id_for(session_id: str) -> str` — 32 lowercase hex
  - `span_id_for(key: str) -> str` — 16 lowercase hex
  - `class Span` with `trace_id, span_id, parent_span_id, name, kind_oi, start_ns, end_ns, attributes, status_code, status_message, pending`
  - `class Ctx` — `session_id, cwd, git_branch, cc_version, service_name, capture_content, max_attr_bytes`
  - `build(entries: List[Entry], state: dict, ctx: Ctx) -> List[Span]` — mutates `state` in place
  - `finalize_turn(state: dict, ctx: Ctx, now_ns: int) -> List[Span]`
  - `finalize_session(state: dict, ctx: Ctx, now_ns: int) -> List[Span]`
  - `truncate(value: str, limit: int) -> str`

`state` is a plain JSON-serializable dict so Task 5 can persist it unchanged:
`{"offset": int, "open_tools": {tool_use_id: {...}}, "open_turns": {prompt_id: {...}}, "root_started": bool, "root_start_ns": int, "last_ns": int, "open_task_spans": [span_id...]}`

**Semconv keys — copy exactly, do not paraphrase:**
`openinference.span.kind`, `gen_ai.operation.name`, `gen_ai.provider.name`, `session.id`,
`gen_ai.request.model`, `gen_ai.response.model`, `gen_ai.usage.input_tokens`,
`gen_ai.usage.output_tokens`, `gen_ai.usage.cache_read.input_tokens`,
`gen_ai.usage.cache_creation.input_tokens`, `gen_ai.response.finish_reasons`,
`gen_ai.tool.name`, `input.value`, `output.value`, `glassflow.span.pending`.

Kind → operation: `AGENT`→`invoke_agent`, `LLM`→`chat`, `TOOL`→`execute_tool`, `CHAIN`→ none.

**Pending-span rule:** a span with `pending=True` carries ONLY these attributes —
`openinference.span.kind`, `gen_ai.operation.name`, `gen_ai.provider.name`,
`gen_ai.tool.name`, `session.id`, any `gen_ai.request.*` — plus
`glassflow.span.pending=True`. Never `input.value`, never `output.value`. This is
an allowlist, not a blocklist.

- [ ] **Step 1: Write the failing test**

```python
import pytest

from rius_cc import spans, transcript


def _ctx(session_id):
    return spans.Ctx(session_id=session_id, cwd="/tmp/proj", git_branch="main",
                     cc_version="2.1.278", service_name="claude-code",
                     capture_content=True, max_attr_bytes=32768)


def _new_state():
    return {"offset": 0, "open_tools": {}, "open_turns": {}, "root_started": False,
            "root_start_ns": 0, "last_ns": 0, "open_task_spans": []}


def test_ids_are_derived_and_stable():
    # Literal expectations: if the derivation ever changes, in-flight sessions
    # orphan their spans. This test exists to make that change loud.
    assert spans.trace_id_for("abc") == spans.trace_id_for("abc")
    assert len(spans.trace_id_for("abc")) == 32
    assert len(spans.span_id_for("abc")) == 16
    assert int(spans.trace_id_for("abc"), 16) >= 0          # valid hex
    assert spans.trace_id_for("abc") != spans.trace_id_for("abd")


def test_root_span_has_empty_parent_and_is_pending_first(fixtures_dir):
    entries, _ = transcript.read_from(str(fixtures_dir / "simple.jsonl"), 0)
    st = _new_state()
    out = spans.build(entries, st, _ctx(entries[0].session_id))
    root = [s for s in out if s.kind_oi == "AGENT"][0]
    assert root.parent_span_id is None
    assert root.pending is True
    assert root.attributes["session.id"] == entries[0].session_id
    assert "input.value" not in root.attributes


def test_generation_span_carries_usage(fixtures_dir):
    entries, _ = transcript.read_from(str(fixtures_dir / "simple.jsonl"), 0)
    st = _new_state()
    out = spans.build(entries, st, _ctx(entries[0].session_id))
    gen = [s for s in out if s.kind_oi == "LLM"][0]
    a = gen.attributes
    assert a["gen_ai.request.model"] == "claude-opus-5"
    assert a["gen_ai.usage.input_tokens"] == 10
    assert a["gen_ai.usage.output_tokens"] == 5
    assert a["gen_ai.usage.cache_read.input_tokens"] == 100
    assert a["gen_ai.usage.cache_creation.input_tokens"] == 200
    assert a["gen_ai.operation.name"] == "chat"
    assert a["gen_ai.response.finish_reasons"] == ["end_turn"]
    assert gen.end_ns > gen.start_ns
    assert gen.pending is False


def test_tool_span_spans_request_to_result(fixtures_dir):
    entries, _ = transcript.read_from(str(fixtures_dir / "tool_call.jsonl"), 0)
    st = _new_state()
    out = spans.build(entries, st, _ctx(entries[0].session_id))
    final_tools = [s for s in out if s.kind_oi == "TOOL" and not s.pending]
    assert len(final_tools) == 1
    tool = final_tools[0]
    assert tool.attributes["gen_ai.tool.name"] == "Read"
    assert tool.end_ns - tool.start_ns == 2_000_000_000   # 10:00:01 -> 10:00:03
    assert tool.status_code != "ERROR"
    assert st["open_tools"] == {}                          # resolved and cleared


def test_tool_error_sets_error_status(fixtures_dir):
    entries, _ = transcript.read_from(str(fixtures_dir / "tool_error.jsonl"), 0)
    st = _new_state()
    out = spans.build(entries, st, _ctx(entries[0].session_id))
    tool = [s for s in out if s.kind_oi == "TOOL" and not s.pending][0]
    assert tool.status_code == "ERROR"


def test_tool_left_open_across_two_builds(fixtures_dir):
    """A tool_use in one batch, its result in the next -- the real hook cadence."""
    entries, _ = transcript.read_from(str(fixtures_dir / "tool_call.jsonl"), 0)
    st = _new_state()
    first = spans.build(entries[:2], st, _ctx(entries[0].session_id))
    assert "toolu_1" in st["open_tools"]
    assert [s for s in first if s.kind_oi == "TOOL"][0].pending is True
    second = spans.build(entries[2:], st, _ctx(entries[0].session_id))
    closed = [s for s in second if s.kind_oi == "TOOL" and not s.pending]
    assert len(closed) == 1
    assert st["open_tools"] == {}


def test_span_ids_identical_across_replay(fixtures_dir):
    entries, _ = transcript.read_from(str(fixtures_dir / "tool_call.jsonl"), 0)
    a = spans.build(entries, _new_state(), _ctx(entries[0].session_id))
    b = spans.build(entries, _new_state(), _ctx(entries[0].session_id))
    assert [s.span_id for s in a] == [s.span_id for s in b]
    assert [s.parent_span_id for s in a] == [s.parent_span_id for s in b]


def test_pending_spans_never_carry_content(fixtures_dir):
    entries, _ = transcript.read_from(str(fixtures_dir / "tool_call.jsonl"), 0)
    out = spans.build(entries[:2], _new_state(), _ctx(entries[0].session_id))
    for s in out:
        if s.pending:
            assert "input.value" not in s.attributes
            assert "output.value" not in s.attributes
            assert s.attributes["glassflow.span.pending"] is True


def test_capture_content_false_strips_content(fixtures_dir):
    entries, _ = transcript.read_from(str(fixtures_dir / "tool_call.jsonl"), 0)
    ctx = _ctx(entries[0].session_id)
    ctx.capture_content = False
    out = spans.build(entries, _new_state(), ctx)
    for s in out:
        assert "input.value" not in s.attributes
        assert "output.value" not in s.attributes
    # structure survives
    assert any(s.attributes.get("gen_ai.tool.name") == "Read" for s in out)


def test_truncate_marks_what_it_removed():
    out = spans.truncate("x" * 100, 10)
    assert out.startswith("x" * 10)
    assert "[truncated 90 bytes]" in out
    assert spans.truncate("short", 100) == "short"


def test_finalize_session_closes_root(fixtures_dir):
    entries, _ = transcript.read_from(str(fixtures_dir / "simple.jsonl"), 0)
    st = _new_state()
    ctx = _ctx(entries[0].session_id)
    spans.build(entries, st, ctx)
    out = spans.finalize_session(st, ctx, now_ns=st["last_ns"] + 1_000_000_000)
    root = [s for s in out if s.kind_oi == "AGENT"][0]
    assert root.pending is False
    assert root.parent_span_id is None
    assert root.end_ns > root.start_ns
    assert root.span_id == spans.span_id_for("session:" + ctx.session_id)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_spans.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'rius_cc.spans'`

- [ ] **Step 3: Write the implementation**

Implement `scripts/rius_cc/spans.py` to satisfy exactly the tests above. Required behaviour, in order of processing each entry:

1. If `state["root_started"]` is False: set it True, record `root_start_ns` = first entry's `timestamp_ns`, and emit the **pending root** — `span_id_for("session:" + session_id)`, `parent_span_id=None`, kind `AGENT`, name `claude-code session`.
2. A **user entry with a `prompt_id` and no `tool_result` blocks** opens a turn if that `prompt_id` is not in `open_turns`: span id `span_id_for("turn:" + prompt_id)`, kind `CHAIN`, name `turn`, parent = root. Emit pending. On a non-pending emission later, `input.value` = the entry's `text()`.
3. An **assistant entry** emits a final `LLM` span: span id `span_id_for(entry.uuid)`, parent = the enclosing turn span (or the innermost open Task span if `entry.is_sidechain`), `start_ns = state["last_ns"] or entry.timestamp_ns`, `end_ns = entry.timestamp_ns`, name = the model. Attributes per the table above; `output.value` = `entry.text()`.
4. For each `tool_use` block on that assistant entry: record `state["open_tools"][block["id"]]` with span id `span_id_for(block["id"])`, parent = the LLM span, `start_ns = entry.timestamp_ns`, tool name, and the JSON-encoded input. Emit a **pending TOOL span**. If the tool name is `Task`, append its span id to `state["open_task_spans"]`.
5. A **user entry with `tool_result` blocks**: for each, pop `state["open_tools"][tool_use_id]` and emit the final `TOOL` span with `end_ns = entry.timestamp_ns`, `output.value` = the result content stringified, and `status_code = "ERROR"` when `is_error` is true (with `status_message` = the content, truncated). Remove the span id from `open_task_spans` if present. A result with no matching open tool is skipped, not raised — it means the tool started before instrumentation was enabled.
6. After each entry, set `state["last_ns"] = entry.timestamp_ns`.

`finalize_turn` closes and emits every span in `open_turns` with `end_ns=now_ns`, then clears it. `finalize_session` calls `finalize_turn`, then emits the final root with `start_ns = state["root_start_ns"]`, `end_ns = now_ns`.

`truncate(value, limit)` returns `value` unchanged when `len(value.encode("utf-8")) <= limit`; otherwise the first `limit` characters plus `" …[truncated N bytes]"` where N is the number of bytes dropped.

Every content attribute is passed through `truncate(..., ctx.max_attr_bytes)`, and is omitted entirely when `ctx.capture_content` is False.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_spans.py -q`
Expected: 11 passed.

- [ ] **Step 5: Commit**

```bash
git add scripts/rius_cc/spans.py tests/test_spans.py
git commit -m "feat: span tree from transcript entries"
```

---

### Task 4: Configuration resolution

**Files:**
- Create: `scripts/rius_cc/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `class Config` — `enabled: bool`, `reason: str`, `api_key: Optional[str]`, `endpoint: str`, `service_name: str`, `capture_content: bool`, `max_attr_bytes: int`, `debug: bool`
  - `resolve(session_id: str, cwd: str, env: Mapping[str, str], home: str) -> Config`
  - `session_override_path(session_id: str, home: str) -> str`
  - `set_session_override(session_id: str, home: str, on: Optional[bool]) -> None` — `None` clears
  - `path_rules_path(home: str) -> str`
  - `redact(api_key: Optional[str]) -> str`

**Resolution order, first match wins** (spec §8.1):
1. session override file `<home>/.claude/rius/sessions/<session_id>` containing `on` or `off`
2. `env["RIUS_CLAUDE_ENABLED"]` — `"true"`/`"1"` on, `"false"`/`"0"` off (this single variable covers spec layers 2 and 3, because Claude Code has already merged `.claude/settings.local.json` over `.claude/settings.json` over the user settings before the hook runs)
3. path rules in `<home>/.claude/rius/config.json`: `{"enabled_paths": [glob...], "disabled_paths": [glob...]}` — `disabled_paths` wins over `enabled_paths`; match with `fnmatch` against `cwd`, and treat a rule as matching any subdirectory of itself
4. default **off**

A missing `RIUS_API_KEY` forces `enabled=False` regardless, with a distinct `reason`.

`reason` is a human sentence naming the deciding layer, e.g.
`"off: no path rule matches /x/y, and the default is off"`,
`"on: session override"`, `"off: RIUS_API_KEY is not set"`.

- [ ] **Step 1: Write the failing test**

```python
import json
import os

from rius_cc import config


def _home(tmp_path):
    os.makedirs(str(tmp_path / ".claude" / "rius"), exist_ok=True)
    return str(tmp_path)


BASE_ENV = {"RIUS_API_KEY": "glassflow_secret"}


def test_default_is_off(tmp_path):
    c = config.resolve("s1", "/x/y", BASE_ENV, _home(tmp_path))
    assert c.enabled is False
    assert "default" in c.reason


def test_missing_api_key_disables_even_when_enabled(tmp_path):
    home = _home(tmp_path)
    config.set_session_override("s1", home, True)
    c = config.resolve("s1", "/x/y", {}, home)
    assert c.enabled is False
    assert "RIUS_API_KEY" in c.reason


def test_session_override_beats_everything(tmp_path):
    home = _home(tmp_path)
    config.set_session_override("s1", home, True)
    env = dict(BASE_ENV, RIUS_CLAUDE_ENABLED="false")
    c = config.resolve("s1", "/x/y", env, home)
    assert c.enabled is True
    assert "session override" in c.reason


def test_session_override_can_disable(tmp_path):
    home = _home(tmp_path)
    config.set_session_override("s1", home, False)
    env = dict(BASE_ENV, RIUS_CLAUDE_ENABLED="true")
    assert config.resolve("s1", "/x/y", env, home).enabled is False


def test_clearing_session_override_falls_through(tmp_path):
    home = _home(tmp_path)
    config.set_session_override("s1", home, True)
    config.set_session_override("s1", home, None)
    env = dict(BASE_ENV, RIUS_CLAUDE_ENABLED="true")
    assert config.resolve("s1", "/x/y", env, home).enabled is True


def test_env_beats_path_rules(tmp_path):
    home = _home(tmp_path)
    with open(config.path_rules_path(home), "w") as fh:
        json.dump({"enabled_paths": ["/x/*"]}, fh)
    env = dict(BASE_ENV, RIUS_CLAUDE_ENABLED="false")
    c = config.resolve("s1", "/x/y", env, home)
    assert c.enabled is False


def test_path_rule_enables_folder_and_subfolders(tmp_path):
    home = _home(tmp_path)
    with open(config.path_rules_path(home), "w") as fh:
        json.dump({"enabled_paths": ["/opt/example"]}, fh)
    assert config.resolve("s1", "/opt/example", BASE_ENV, home).enabled is True
    assert config.resolve("s1", "/opt/example/deep/dir", BASE_ENV, home).enabled is True
    assert config.resolve("s1", "/opt/other", BASE_ENV, home).enabled is False


def test_disabled_path_beats_enabled_path(tmp_path):
    home = _home(tmp_path)
    with open(config.path_rules_path(home), "w") as fh:
        json.dump({"enabled_paths": ["/opt/*"],
                   "disabled_paths": ["/opt/secret"]}, fh)
    assert config.resolve("s1", "/opt/ok", BASE_ENV, home).enabled is True
    assert config.resolve("s1", "/opt/secret", BASE_ENV, home).enabled is False


def test_corrupt_path_rules_do_not_raise(tmp_path):
    home = _home(tmp_path)
    with open(config.path_rules_path(home), "w") as fh:
        fh.write("{not json")
    c = config.resolve("s1", "/x/y", BASE_ENV, home)
    assert c.enabled is False


def test_defaults_and_overrides(tmp_path):
    home = _home(tmp_path)
    c = config.resolve("s1", "/x", BASE_ENV, home)
    assert c.endpoint == "https://ingest.eu.console.rius-glassflow.com"
    assert c.service_name == "claude-code"
    assert c.capture_content is True
    assert c.max_attr_bytes == 32768
    env = dict(BASE_ENV, RIUS_ENDPOINT="https://ingest.staging.rius.glassflow.xyz",
               RIUS_SERVICE_NAME="cc-dev", RIUS_CAPTURE_CONTENT="false",
               RIUS_CLAUDE_MAX_ATTR_BYTES="100")
    c = config.resolve("s1", "/x", env, home)
    assert c.endpoint == "https://ingest.staging.rius.glassflow.xyz"
    assert c.service_name == "cc-dev"
    assert c.capture_content is False
    assert c.max_attr_bytes == 100


def test_redact_never_leaks_the_key():
    assert config.redact("glassflow_abcdef123456") == "glassflow_…"
    assert "abcdef" not in config.redact("glassflow_abcdef123456")
    assert config.redact(None) == "<unset>"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_config.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'rius_cc.config'`

- [ ] **Step 3: Write the implementation**

Implement `scripts/rius_cc/config.py` to satisfy the tests. Notes that are easy to get wrong:

- Path matching: a rule matches when `cwd == rule`, when `cwd.startswith(rule.rstrip("/") + "/")`, or when `fnmatch.fnmatch(cwd, rule)`. The first two are what make `"/opt/example"` cover `/opt/example/deep/dir`; the third is what makes `"/x/*"` work.
- Every file read is wrapped — a corrupt rules file resolves to the default, never an exception.
- `max_attr_bytes` parses with a fallback to 32768 on a non-integer value.
- `redact` returns `"<unset>"` for `None` and otherwise `"glassflow_…"`, never any part of the secret.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_config.py -q`
Expected: 11 passed.

- [ ] **Step 5: Commit**

```bash
git add scripts/rius_cc/config.py tests/test_config.py
git commit -m "feat: config resolution ladder, default off"
```

---

### Task 5: Session state and locking

**Files:**
- Create: `scripts/rius_cc/state.py`
- Test: `tests/test_state.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `new_state() -> dict` — `{"offset": 0, "open_tools": {}, "open_turns": {}, "root_started": False, "root_start_ns": 0, "last_ns": 0, "open_task_spans": []}`
  - `load(session_id: str, home: str) -> dict`
  - `save(session_id: str, home: str, state: dict) -> None` — atomic (temp file + `os.replace`)
  - `session_lock(session_id: str, home: str)` — context manager yielding `True` if acquired, `False` if another process holds it

- [ ] **Step 1: Write the failing test**

```python
import multiprocessing
import os
import time

from rius_cc import state


def test_new_state_shape():
    s = state.new_state()
    assert s["offset"] == 0
    assert s["open_tools"] == {}
    assert s["root_started"] is False


def test_load_missing_returns_new(tmp_path):
    assert state.load("nope", str(tmp_path))["offset"] == 0


def test_save_then_load_roundtrip(tmp_path):
    s = state.new_state()
    s["offset"] = 42
    s["open_tools"]["toolu_1"] = {"span_id": "aabb", "start_ns": 1}
    state.save("s1", str(tmp_path), s)
    back = state.load("s1", str(tmp_path))
    assert back["offset"] == 42
    assert back["open_tools"]["toolu_1"]["span_id"] == "aabb"


def test_corrupt_state_resets_instead_of_raising(tmp_path):
    state.save("s1", str(tmp_path), state.new_state())
    path = state.state_path("s1", str(tmp_path))
    with open(path, "w") as fh:
        fh.write("{broken")
    assert state.load("s1", str(tmp_path))["offset"] == 0


def test_save_is_atomic_leaving_no_temp_files(tmp_path):
    state.save("s1", str(tmp_path), state.new_state())
    d = os.path.dirname(state.state_path("s1", str(tmp_path)))
    assert [f for f in os.listdir(d) if f.endswith(".tmp")] == []


def test_lock_is_exclusive(tmp_path):
    home = str(tmp_path)
    with state.session_lock("s1", home) as got:
        assert got is True
        with state.session_lock("s1", home) as second:
            assert second is False


def test_lock_released_after_block(tmp_path):
    home = str(tmp_path)
    with state.session_lock("s1", home) as got:
        assert got is True
    with state.session_lock("s1", home) as again:
        assert again is True


def test_different_sessions_do_not_block_each_other(tmp_path):
    home = str(tmp_path)
    with state.session_lock("s1", home) as a:
        with state.session_lock("s2", home) as b:
            assert a is True and b is True
```

Note `test_lock_is_exclusive` relies on `flock` being per-file-descriptor: the
nested `open()` produces a new descriptor, so `LOCK_EX | LOCK_NB` fails even in
the same process. If the implementation caches descriptors per path, this test
fails — and that caching would be a real bug, because it would let two exporters
in the same process interleave.

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_state.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'rius_cc.state'`

- [ ] **Step 3: Write the implementation**

Implement `scripts/rius_cc/state.py`:

- `state_dir(home)` is `<home>/.claude/rius/state`, created with `os.makedirs(..., exist_ok=True)`.
- `state_path(session_id, home)` is `<state_dir>/<session_id>.json`; `lock_path` is `<state_dir>/<session_id>.lock`.
- `save` writes to `<path>.tmp` then `os.replace(tmp, path)` — never a partial file a concurrent reader could see.
- `load` returns `new_state()` on any `OSError` or `ValueError`, and fills in any key missing from an older state file so a plugin upgrade cannot crash on an old file.
- `session_lock` is a `@contextlib.contextmanager` that opens the lock file, tries `fcntl.flock(fd, LOCK_EX | LOCK_NB)`, yields True on success and False on `OSError`, and always closes the descriptor in a `finally`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_state.py -q`
Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
git add scripts/rius_cc/state.py tests/test_state.py
git commit -m "feat: per-session state with atomic save and flock"
```

---

### Task 6: OTLP encoding and export

**Files:**
- Create: `scripts/rius_cc/otlp.py`
- Test: `tests/test_otlp.py`, `tests/test_otlp_roundtrip.py`

**Interfaces:**
- Consumes: `proto` (Task 1), `spans.Span` (Task 3).
- Produces:
  - `encode(resource_attrs: Dict[str, object], span_list: List[Span]) -> bytes` — a complete `ExportTraceServiceRequest`
  - `export(endpoint: str, api_key: str, body: bytes, timeout: float = 5.0) -> int` — returns the HTTP status, or 0 on a transport failure; retries once
  - `SCOPE_NAME = "glassflow"`

**Field numbers — verified against `opentelemetry-proto==1.43.0` and byte-checked
against the reference serializer. Do not adjust these from memory.**

```
ExportTraceServiceRequest / TracesData: 1 resource_spans (repeated msg)   [identical on the wire]
ResourceSpans:   1 resource (msg) · 2 scope_spans (repeated msg) · 3 schema_url (string)
ScopeSpans:      1 scope (msg) · 2 spans (repeated msg) · 3 schema_url (string)
Span:            1 trace_id (bytes,16) · 2 span_id (bytes,8) · 3 trace_state (string)
                 4 parent_span_id (bytes,8) · 5 name (string) · 6 kind (enum varint)
                 7 start_time_unix_nano (FIXED64) · 8 end_time_unix_nano (FIXED64)
                 9 attributes (repeated KeyValue) · 10 dropped_attributes_count (uint32)
                 11 events (repeated Event) · 15 status (msg)
Span.Event:      1 time_unix_nano (FIXED64) · 2 name (string) · 3 attributes (repeated KeyValue)
Status:          2 message (string) · 3 code (enum varint)      [FIELD 1 IS RESERVED - NEVER EMIT]
Resource:        1 attributes (repeated KeyValue)               [do NOT emit field 3 entity_refs]
InstrumentationScope: 1 name (string) · 2 version (string) · 3 attributes (repeated KeyValue)
KeyValue:        1 key (string) · 2 value (AnyValue)            [do NOT emit field 3 key_strindex]
AnyValue:        1 string_value (string) · 2 bool_value (varint) · 3 int_value (int64 varint)
                 4 double_value (DOUBLE, wire type 1) · 5 array_value (msg) · 7 bytes_value (bytes)
ArrayValue:      1 values (repeated AnyValue)
SpanKind enum:   UNSPECIFIED=0 INTERNAL=1 SERVER=2 CLIENT=3 PRODUCER=4 CONSUMER=5
StatusCode enum: UNSET=0 OK=1 ERROR=2
```

All spans we emit use OTel `SpanKind.INTERNAL` (1). The Rius/OpenInference kind
travels in the `openinference.span.kind` attribute, not in this enum — they are
different taxonomies and conflating them is the easy mistake here.

- [ ] **Step 1: Write the encoding test**

```python
import struct

from rius_cc import otlp, spans


def _span(**kw):
    s = spans.Span(
        trace_id="a" * 32, span_id="b" * 16, parent_span_id=None,
        name="n", kind_oi="LLM", start_ns=1_000_000_000, end_ns=2_000_000_000,
        attributes={}, status_code=None, status_message=None, pending=False)
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def test_ids_are_raw_bytes_not_hex_text():
    body = otlp.encode({}, [_span()])
    assert bytes.fromhex("a" * 32) in body
    assert b"a" * 32 not in body        # the hex TEXT must never appear


def test_timestamps_are_fixed64_little_endian():
    body = otlp.encode({}, [_span(start_ns=1758535200000000000)])
    assert b"\x39" + struct.pack("<Q", 1758535200000000000) in body


def test_scope_name_is_glassflow():
    assert otlp.SCOPE_NAME == "glassflow"
    assert b"glassflow" in otlp.encode({}, [_span()])


def test_root_span_omits_parent_field():
    body = otlp.encode({}, [_span(parent_span_id=None)])
    assert b"\x22" not in body          # tag for field 4, parent_span_id


def test_attribute_types_encode_distinctly():
    body = otlp.encode({}, [_span(attributes={
        "s": "txt", "i": 7, "b": True, "d": 1.5, "arr": ["x", "y"]})])
    assert b"txt" in body and b"arr" in body


def test_empty_span_list_is_empty_body():
    assert otlp.encode({}, []) == b""
```

- [ ] **Step 2: Write the round-trip test — this is the real guarantee**

```python
"""Decode our hand-rolled encoder's output with the REAL library.

opentelemetry-proto is a test-only dependency. It never ships. Its entire job
is to make it impossible for a field number or wire type to drift silently.
"""
import pytest

from rius_cc import otlp, spans

pytest.importorskip("opentelemetry.proto.trace.v1.trace_pb2")

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (  # noqa: E402
    ExportTraceServiceRequest,
)


def _decode(body):
    req = ExportTraceServiceRequest()
    req.ParseFromString(body)
    return req


def test_roundtrip_full_span():
    s = spans.Span(
        trace_id="0123456789abcdef0123456789abcdef",
        span_id="fedcba9876543210",
        parent_span_id="1122334455667788",
        name="claude-opus-5", kind_oi="LLM",
        start_ns=1758535200000000000, end_ns=1758535202500000000,
        attributes={
            "gen_ai.request.model": "claude-opus-5",
            "gen_ai.usage.input_tokens": 10,
            "gen_ai.usage.output_tokens": 5,
            "glassflow.span.pending": False,
            "gen_ai.response.finish_reasons": ["end_turn"],
        },
        status_code="ERROR", status_message="boom", pending=False)

    req = _decode(otlp.encode({"service.name": "claude-code"}, [s]))

    rs = req.resource_spans[0]
    assert {kv.key: kv.value.string_value for kv in rs.resource.attributes} == {
        "service.name": "claude-code"}
    ss = rs.scope_spans[0]
    assert ss.scope.name == "glassflow"

    got = ss.spans[0]
    assert got.trace_id == bytes.fromhex("0123456789abcdef0123456789abcdef")
    assert got.span_id == bytes.fromhex("fedcba9876543210")
    assert got.parent_span_id == bytes.fromhex("1122334455667788")
    assert got.name == "claude-opus-5"
    assert got.start_time_unix_nano == 1758535200000000000
    assert got.end_time_unix_nano == 1758535202500000000
    assert got.status.code == 2                      # ERROR
    assert got.status.message == "boom"

    attrs = {kv.key: kv.value for kv in got.attributes}
    assert attrs["gen_ai.request.model"].string_value == "claude-opus-5"
    assert attrs["gen_ai.usage.input_tokens"].int_value == 10
    assert attrs["glassflow.span.pending"].bool_value is False
    assert [v.string_value
            for v in attrs["gen_ai.response.finish_reasons"].array_value.values] == ["end_turn"]


def test_roundtrip_root_span_has_empty_parent():
    s = spans.Span(trace_id="a" * 32, span_id="b" * 16, parent_span_id=None,
                   name="claude-code session", kind_oi="AGENT",
                   start_ns=1, end_ns=2, attributes={}, status_code=None,
                   status_message=None, pending=True)
    got = _decode(otlp.encode({}, [s])).resource_spans[0].scope_spans[0].spans[0]
    assert got.parent_span_id == b""


def test_roundtrip_many_spans_preserves_order():
    made = [spans.Span(trace_id="a" * 32, span_id="%016x" % i,
                       parent_span_id=None, name="s%d" % i, kind_oi="CHAIN",
                       start_ns=i + 1, end_ns=i + 2, attributes={},
                       status_code=None, status_message=None, pending=False)
            for i in range(25)]
    got = _decode(otlp.encode({}, made)).resource_spans[0].scope_spans[0].spans
    assert [s.name for s in got] == ["s%d" % i for i in range(25)]
```

- [ ] **Step 3: Run both tests to verify they fail**

Run: `python3 -m pytest tests/test_otlp.py tests/test_otlp_roundtrip.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'rius_cc.otlp'`

- [ ] **Step 4: Write the implementation**

Implement `scripts/rius_cc/otlp.py` using `proto` primitives only. Build bottom-up: `AnyValue` → `KeyValue` → `Span` → `ScopeSpans` → `ResourceSpans` → request.

Value dispatch for `AnyValue`, in this order (`bool` before `int` — `bool` is a subclass of `int` in Python, and getting this backwards encodes `True` as `1`):
`bool` → field 2; `int` → field 3; `float` → field 4; `list`/`tuple` → field 5 `ArrayValue`; everything else → field 1, `str(value)`.

`encode` returns `b""` for an empty span list — there is nothing to POST.

`export` POSTs with `Content-Type: application/x-protobuf` and
`Authorization: Bearer <api_key>`, using `urllib.request`. It retries once on a
transport error or a 5xx, then gives up and returns 0 or the last status. It
never raises, and never includes the key in any exception text it logs.

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_otlp.py tests/test_otlp_roundtrip.py -q`
Expected: 9 passed.

- [ ] **Step 6: Commit**

```bash
git add scripts/rius_cc/otlp.py tests/test_otlp.py tests/test_otlp_roundtrip.py
git commit -m "feat: OTLP protobuf encoding verified against opentelemetry-proto"
```

---

### Task 7: The exporter

**Files:**
- Create: `scripts/exporter.py`
- Test: `tests/test_exporter.py`

**Interfaces:**
- Consumes: `config`, `state`, `transcript`, `spans`, `otlp` (Tasks 2–6).
- Produces: `run(event: str, payload: dict, env: Mapping[str, str], home: str) -> int` — returns the number of spans exported. Also `main()`, which reads its payload from `sys.argv[1]` (a file path) and always exits 0.

The hook passes the payload via a temp file rather than stdin, because the
exporter is detached and its parent has already exited — there is nobody left
to hold a pipe open.

- [ ] **Step 1: Write the failing test**

```python
import json

import pytest

import exporter
from rius_cc import config, state


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    (h / ".claude" / "rius").mkdir(parents=True)
    with open(config.path_rules_path(str(h)), "w") as fh:
        json.dump({"enabled_paths": ["/tmp"]}, fh)
    return str(h)


@pytest.fixture
def captured(monkeypatch):
    sent = []
    monkeypatch.setattr(exporter.otlp, "export",
                        lambda ep, key, body, timeout=5.0: sent.append((ep, key, body)) or 200)
    return sent


def _payload(fixtures_dir, name, session_id, event, cwd="/tmp/proj"):
    return {"session_id": session_id, "transcript_path": str(fixtures_dir / name),
            "cwd": cwd, "hook_event_name": event}


ENV = {"RIUS_API_KEY": "glassflow_k", "RIUS_ENDPOINT": "https://ingest.test"}


def test_exports_spans_and_advances_offset(home, captured, fixtures_dir):
    sid = "11111111-1111-1111-1111-111111111111"
    n = exporter.run("PostToolUse", _payload(fixtures_dir, "simple.jsonl", sid, "PostToolUse"), ENV, home)
    assert n > 0
    assert len(captured) == 1
    assert captured[0][0] == "https://ingest.test"
    assert state.load(sid, home)["offset"] > 0


def test_second_run_with_no_new_lines_sends_nothing(home, captured, fixtures_dir):
    sid = "11111111-1111-1111-1111-111111111111"
    p = _payload(fixtures_dir, "simple.jsonl", sid, "PostToolUse")
    exporter.run("PostToolUse", p, ENV, home)
    before = len(captured)
    assert exporter.run("PostToolUse", p, ENV, home) == 0
    assert len(captured) == before


def test_disabled_folder_exports_nothing(home, captured, fixtures_dir):
    sid = "11111111-1111-1111-1111-111111111111"
    p = _payload(fixtures_dir, "simple.jsonl", sid, "PostToolUse", cwd="/elsewhere")
    assert exporter.run("PostToolUse", p, ENV, home) == 0
    assert captured == []


def test_session_end_closes_the_root(home, captured, fixtures_dir):
    sid = "11111111-1111-1111-1111-111111111111"
    exporter.run("PostToolUse", _payload(fixtures_dir, "simple.jsonl", sid, "PostToolUse"), ENV, home)
    captured.clear()
    n = exporter.run("SessionEnd", _payload(fixtures_dir, "simple.jsonl", sid, "SessionEnd"), ENV, home)
    assert n >= 1          # at minimum, the final root span


def test_never_raises_on_a_missing_transcript(home, captured):
    p = {"session_id": "s9", "transcript_path": "/nope/missing.jsonl",
         "cwd": "/tmp/proj", "hook_event_name": "Stop"}
    assert exporter.run("Stop", p, ENV, home) == 0


def test_never_raises_on_a_garbage_payload(home, captured):
    assert exporter.run("Stop", {}, ENV, home) == 0


def test_lock_contention_is_a_no_op(home, captured, fixtures_dir):
    sid = "11111111-1111-1111-1111-111111111111"
    with state.session_lock(sid, home):
        n = exporter.run("PostToolUse",
                         _payload(fixtures_dir, "simple.jsonl", sid, "PostToolUse"), ENV, home)
    assert n == 0
    assert captured == []


def test_api_key_never_appears_in_the_log(home, captured, fixtures_dir):
    sid = "11111111-1111-1111-1111-111111111111"
    env = dict(ENV, RIUS_CLAUDE_DEBUG="true")
    exporter.run("PostToolUse", _payload(fixtures_dir, "simple.jsonl", sid, "PostToolUse"), env, home)
    import pathlib
    logs = list(pathlib.Path(home, ".claude", "rius", "log").glob("*.log"))
    for log in logs:
        assert "glassflow_k" not in log.read_text()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_exporter.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'exporter'`

- [ ] **Step 3: Write the implementation**

`scripts/exporter.py`, `run()` in order:

1. `cfg = config.resolve(payload["session_id"], payload["cwd"], env, home)`. If not `cfg.enabled`, log the reason at debug level and return 0.
2. Acquire `state.session_lock`. If not acquired, return 0 — the holder reads to EOF and will pick up these lines.
3. `st = state.load(session_id, home)`.
4. `entries, offset = transcript.read_from(payload["transcript_path"], st["offset"])`.
5. Build `ctx` from `cfg` and the first entry's `cwd`/`git_branch`/`cc_version`, falling back to the payload's `cwd`.
6. `out = spans.build(entries, st, ctx)`.
7. If `event == "Stop"`: `out += spans.finalize_turn(st, ctx, now_ns)`.
   If `event == "SessionEnd"`: `out += spans.finalize_session(st, ctx, now_ns)`.
   `now_ns` is `time.time_ns()` when available, else `int(time.time() * 1e9)`.
8. If `out` is non-empty: `body = otlp.encode(resource_attrs, out)` and `otlp.export(...)`.
   Resource attributes: `service.name`, `service.instance.id` (a UUID stored in `st` on first use so it is stable for the session), `cc.version`, `cc.cwd`, `cc.git_branch`.
9. `st["offset"] = offset`; `state.save(...)`. **Save the offset only after a successful export**, so a failed export is retried on the next hook rather than silently skipped.
10. Return `len(out)`.

The whole body of `run` is inside `try/except BaseException` — it logs and
returns 0. `main()` reads `sys.argv[1]` as a JSON file, calls `run`, unlinks the
file, and calls `sys.exit(0)` unconditionally.

The debug log is `<home>/.claude/rius/log/<date>.log`, and every line passes
through `config.redact` for anything key-shaped.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_exporter.py -q`
Expected: 8 passed.

- [ ] **Step 5: Run the whole suite**

Run: `python3 -m pytest -q`
Expected: all tests from Tasks 1–7 pass.

- [ ] **Step 6: Commit**

```bash
git add scripts/exporter.py tests/test_exporter.py
git commit -m "feat: exporter wiring transcript to OTLP export"
```

---

### Task 8: Hook entry point and plugin manifest

**Files:**
- Create: `scripts/hook.py`, `hooks/hooks.json`, `.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json`
- Test: `tests/test_hook.py`

**Interfaces:**
- Consumes: `config` (Task 4), `exporter` (Task 7).
- Produces: `scripts/hook.py`, invoked as `hook.py <EventName>`, reading the hook payload as JSON on stdin.

**Verified hook payload fields** (confirmed against plugins installed on this machine — do not substitute names from memory):

| Event | Fields beyond the common set |
|---|---|
| all | `session_id`, `transcript_path`, `cwd`, `hook_event_name`, `permission_mode` |
| `SessionStart` | `source` (`startup`/`resume`/`clear`/`compact`) |
| `UserPromptSubmit` | `prompt` |
| `PreToolUse` | `tool_name`, `tool_input`, `tool_use_id` |
| `PostToolUse` | `tool_name`, `tool_input`, `tool_response`, `tool_use_id` |
| `Stop` | `stop_hook_active` |
| `SessionEnd` | `reason` |

`hook.py` reads only `session_id`, `transcript_path`, `cwd`. The others are
available but unused — the transcript is the data source (spec §2).

- [ ] **Step 1: Write the failing test**

```python
import json
import subprocess
import sys
import pathlib

HOOK = str(pathlib.Path(__file__).parent.parent / "scripts" / "hook.py")


def _run(event, payload, env=None):
    return subprocess.run(
        [sys.executable, HOOK, event],
        input=json.dumps(payload), capture_output=True, text=True,
        env=env, timeout=30)


def test_exits_zero_on_a_normal_payload(tmp_path):
    r = _run("SessionStart", {"session_id": "s1", "cwd": str(tmp_path),
                              "transcript_path": "/nonexistent.jsonl"})
    assert r.returncode == 0


def test_exits_zero_on_invalid_json_stdin():
    r = subprocess.run([sys.executable, HOOK, "Stop"], input="not json",
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0


def test_exits_zero_on_empty_stdin():
    r = subprocess.run([sys.executable, HOOK, "Stop"], input="",
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0


def test_exits_zero_with_no_argument():
    r = subprocess.run([sys.executable, HOOK], input="{}",
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0


def test_prints_nothing_on_stdout_when_disabled(tmp_path):
    """stdout is a control channel for hooks -- stray output is a protocol bug."""
    r = _run("Stop", {"session_id": "s1", "cwd": str(tmp_path),
                      "transcript_path": "/nonexistent.jsonl"})
    assert r.stdout.strip() == ""


def test_returns_fast(tmp_path):
    import time
    start = time.time()
    _run("PostToolUse", {"session_id": "s1", "cwd": str(tmp_path),
                         "transcript_path": "/nonexistent.jsonl"})
    assert time.time() - start < 2.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_hook.py -q`
Expected: FAIL — the script does not exist.

- [ ] **Step 3: Write `scripts/hook.py`**

```python
#!/usr/bin/env python3
"""Claude Code hook entry point. Runs in the session's critical path.

Does as little as possible: resolve config, and if enabled, hand off to a
DETACHED exporter and exit. Never writes to stdout -- stdout is a control
channel for hooks. Never exits non-zero -- instrumentation that can break the
session it observes is worse than no instrumentation.
"""
import json
import os
import subprocess
import sys
import tempfile


def main() -> None:
    try:
        event = sys.argv[1] if len(sys.argv) > 1 else ""
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict) or not payload.get("session_id"):
            return

        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from rius_cc import config

        home = os.path.expanduser("~")
        cfg = config.resolve(payload.get("session_id", ""),
                             payload.get("cwd", ""), os.environ, home)
        if not cfg.enabled:
            return

        fd, path = tempfile.mkstemp(prefix="rius-hook-", suffix=".json")
        with os.fdopen(fd, "w") as fh:
            json.dump({"event": event, "payload": payload}, fh)

        exporter = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "exporter.py")
        subprocess.Popen(
            [sys.executable, exporter, path],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,      # detach: outlives this process
            close_fds=True,
        )
    except BaseException:
        pass


if __name__ == "__main__":
    main()
    sys.exit(0)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_hook.py -q`
Expected: 6 passed.

- [ ] **Step 5: Write `hooks/hooks.json`**

Schema confirmed against real installed plugins. `${CLAUDE_PLUGIN_ROOT}` **is**
expanded in the `command` string; `matcher` is optional and omitted means all.

```json
{
  "description": "Stream this Claude Code session to Rius as OTLP traces",
  "hooks": {
    "SessionStart": [
      { "hooks": [ { "type": "command", "command": "\"${CLAUDE_PLUGIN_ROOT}/scripts/hook.py\" SessionStart", "shell": "bash" } ] }
    ],
    "UserPromptSubmit": [
      { "hooks": [ { "type": "command", "command": "\"${CLAUDE_PLUGIN_ROOT}/scripts/hook.py\" UserPromptSubmit", "shell": "bash" } ] }
    ],
    "PreToolUse": [
      { "hooks": [ { "type": "command", "command": "\"${CLAUDE_PLUGIN_ROOT}/scripts/hook.py\" PreToolUse", "shell": "bash" } ] }
    ],
    "PostToolUse": [
      { "hooks": [ { "type": "command", "command": "\"${CLAUDE_PLUGIN_ROOT}/scripts/hook.py\" PostToolUse", "shell": "bash" } ] }
    ],
    "Stop": [
      { "hooks": [ { "type": "command", "command": "\"${CLAUDE_PLUGIN_ROOT}/scripts/hook.py\" Stop", "shell": "bash" } ] }
    ],
    "SessionEnd": [
      { "hooks": [ { "type": "command", "command": "\"${CLAUDE_PLUGIN_ROOT}/scripts/hook.py\" SessionEnd", "shell": "bash" } ] }
    ]
  }
}
```

Then `chmod +x scripts/hook.py`.

- [ ] **Step 6: Write the plugin and marketplace manifests**

`.claude-plugin/plugin.json`:

```json
{
  "name": "rius-claude-code",
  "description": "Stream Claude Code sessions to Rius as OTLP GenAI traces",
  "version": "0.1.0",
  "author": { "name": "GlassFlow", "url": "https://glassflow.dev" },
  "homepage": "https://github.com/glassflow/rius-coding-agents"
}
```

`.claude-plugin/marketplace.json`:

```json
{
  "name": "rius-coding-agents",
  "description": "Rius observability for coding agents",
  "owner": { "name": "GlassFlow", "url": "https://glassflow.dev" },
  "plugins": [
    {
      "name": "rius-claude-code",
      "description": "Stream Claude Code sessions to Rius as OTLP GenAI traces",
      "source": "./",
      "version": "0.1.0",
      "category": "monitoring",
      "tags": ["observability", "otel", "tracing", "rius"]
    }
  ]
}
```

`"source": "./"` serves the plugin from this same repository.

- [ ] **Step 7: Verify the manifests parse**

Run: `python3 -c "import json;[json.load(open(p)) for p in ['.claude-plugin/plugin.json','.claude-plugin/marketplace.json','hooks/hooks.json']];print('ok')"`
Expected: `ok`

- [ ] **Step 8: Commit**

```bash
git add scripts/hook.py hooks/hooks.json .claude-plugin tests/test_hook.py
git commit -m "feat: hook entry point and plugin manifests"
```

---

### Task 9: The /rius command

**Files:**
- Create: `commands/rius.md`, `scripts/rius_ctl.py`
- Test: `tests/test_rius_ctl.py`

**Interfaces:**
- Consumes: `config` (Task 4).
- Produces: `scripts/rius_ctl.py`, a CLI taking `on|off|clear|enable-here|status` plus `--session <id>` and `--cwd <path>`, printing human-readable text to stdout.

Command files are `commands/*.md` with `description` / `allowed-tools`
frontmatter and `` !`cmd` `` shell interpolation — confirmed against real
installed plugins. There is no subcommand mechanism, so one command file takes
`$ARGUMENTS`.

- [ ] **Step 1: Write the failing test**

```python
import json
import subprocess
import sys
import pathlib

CTL = str(pathlib.Path(__file__).parent.parent / "scripts" / "rius_ctl.py")


def _run(args, home, env=None):
    e = {"HOME": home, "PATH": "/usr/bin:/bin"}
    e.update(env or {})
    return subprocess.run([sys.executable, CTL] + args,
                          capture_output=True, text=True, env=e, timeout=30)


def test_status_explains_why_it_is_off(tmp_path):
    home = str(tmp_path)
    (tmp_path / ".claude" / "rius").mkdir(parents=True)
    r = _run(["status", "--session", "s1", "--cwd", "/x/y"], home,
             {"RIUS_API_KEY": "glassflow_k"})
    assert r.returncode == 0
    assert "off" in r.stdout.lower()
    assert "/x/y" in r.stdout or "default" in r.stdout.lower()


def test_status_never_prints_the_key(tmp_path):
    home = str(tmp_path)
    (tmp_path / ".claude" / "rius").mkdir(parents=True)
    r = _run(["status", "--session", "s1", "--cwd", "/x"], home,
             {"RIUS_API_KEY": "glassflow_supersecret"})
    assert "supersecret" not in r.stdout


def test_on_then_status_reports_on(tmp_path):
    home = str(tmp_path)
    (tmp_path / ".claude" / "rius").mkdir(parents=True)
    _run(["on", "--session", "s1", "--cwd", "/x"], home, {"RIUS_API_KEY": "glassflow_k"})
    r = _run(["status", "--session", "s1", "--cwd", "/x"], home, {"RIUS_API_KEY": "glassflow_k"})
    assert "on" in r.stdout.lower()


def test_enable_here_adds_a_path_rule(tmp_path):
    home = str(tmp_path)
    (tmp_path / ".claude" / "rius").mkdir(parents=True)
    _run(["enable-here", "--session", "s1", "--cwd", "/opt/proj"], home,
         {"RIUS_API_KEY": "glassflow_k"})
    rules = json.load(open(str(tmp_path / ".claude" / "rius" / "config.json")))
    assert "/opt/proj" in rules["enabled_paths"]


def test_enable_here_is_idempotent(tmp_path):
    home = str(tmp_path)
    (tmp_path / ".claude" / "rius").mkdir(parents=True)
    for _ in range(3):
        _run(["enable-here", "--session", "s1", "--cwd", "/opt/proj"], home,
             {"RIUS_API_KEY": "glassflow_k"})
    rules = json.load(open(str(tmp_path / ".claude" / "rius" / "config.json")))
    assert rules["enabled_paths"].count("/opt/proj") == 1


def test_unknown_action_exits_zero_with_usage(tmp_path):
    home = str(tmp_path)
    (tmp_path / ".claude" / "rius").mkdir(parents=True)
    r = _run(["wat", "--session", "s1", "--cwd", "/x"], home)
    assert r.returncode == 0
    assert "usage" in r.stdout.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_rius_ctl.py -q`
Expected: FAIL — the script does not exist.

- [ ] **Step 3: Write `scripts/rius_ctl.py`**

Implement the five actions. `status` prints the resolved state, the deciding
layer from `cfg.reason`, the endpoint, the redacted key, and the spans exported
so far for this session (read from the state file). It must make "off because
nothing opted this folder in" unmistakable — that is the failure mode a
default-off design creates (spec §8.2). Every action exits 0.

- [ ] **Step 4: Write `commands/rius.md`**

```markdown
---
description: Control Rius tracing for this Claude Code session
allowed-tools: Bash(${CLAUDE_PLUGIN_ROOT}/scripts/rius_ctl.py:*)
---

!`"${CLAUDE_PLUGIN_ROOT}/scripts/rius_ctl.py" ${ARGUMENTS:-status} --cwd "$PWD"`

Report the output above to the user verbatim. Do not add interpretation.
```

Note the command cannot know the session id, so `rius_ctl.py` resolves it from
the most recently modified state file when `--session` is absent, and says so
when it cannot.

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_rius_ctl.py -q`
Expected: 6 passed.

- [ ] **Step 6: Commit**

```bash
git add scripts/rius_ctl.py commands/rius.md tests/test_rius_ctl.py
git commit -m "feat: /rius command for per-session and per-folder control"
```

---

### Task 10: End-to-end against a fake receiver

**Files:**
- Create: `tests/test_e2e.py`

**Interfaces:**
- Consumes: everything.
- Produces: nothing importable — this is the integration gate.

- [ ] **Step 1: Write the test**

```python
"""Full path: hook -> detached exporter -> HTTP POST, against a real socket.

A fake OTLP receiver on localhost, decoding with opentelemetry-proto. This is
the only test that exercises process detachment, so it is the only one that
would catch a hook that spawns an exporter which never runs.
"""
import json
import pathlib
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

pytest.importorskip("opentelemetry.proto.trace.v1.trace_pb2")
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (  # noqa: E402
    ExportTraceServiceRequest,
)

ROOT = pathlib.Path(__file__).parent.parent
HOOK = str(ROOT / "scripts" / "hook.py")

received = []


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        received.append({"path": self.path,
                         "auth": self.headers.get("Authorization"),
                         "ctype": self.headers.get("Content-Type"),
                         "body": body})
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *a):
        pass


@pytest.fixture
def server():
    received.clear()
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield "http://127.0.0.1:%d" % srv.server_address[1]
    srv.shutdown()


def test_hook_to_receiver_full_path(server, tmp_path, fixtures_dir):
    home = tmp_path / "home"
    (home / ".claude" / "rius").mkdir(parents=True)
    with open(home / ".claude" / "rius" / "config.json", "w") as fh:
        json.dump({"enabled_paths": ["/tmp"]}, fh)

    payload = {
        "session_id": "11111111-1111-1111-1111-111111111111",
        "transcript_path": str(fixtures_dir / "tool_call.jsonl"),
        "cwd": "/tmp/proj",
        "hook_event_name": "PostToolUse",
    }
    env = {
        "HOME": str(home), "PATH": "/usr/bin:/bin",
        "RIUS_API_KEY": "glassflow_testkey",
        "RIUS_ENDPOINT": server,
    }
    r = subprocess.run([sys.executable, HOOK, "PostToolUse"],
                       input=json.dumps(payload), capture_output=True,
                       text=True, env=env, timeout=30)
    assert r.returncode == 0

    deadline = time.time() + 15
    while not received and time.time() < deadline:
        time.sleep(0.05)
    assert received, "detached exporter never POSTed"

    got = received[0]
    assert got["path"] == "/v1/traces"
    assert got["auth"] == "Bearer glassflow_testkey"
    assert got["ctype"] == "application/x-protobuf"

    req = ExportTraceServiceRequest()
    req.ParseFromString(got["body"])
    spans_out = req.resource_spans[0].scope_spans[0].spans
    assert req.resource_spans[0].scope_spans[0].scope.name == "glassflow"

    kinds = {}
    for s in spans_out:
        attrs = {kv.key: kv.value.string_value for kv in s.attributes}
        kinds.setdefault(attrs.get("openinference.span.kind"), []).append(s)
    assert "AGENT" in kinds        # session root
    assert "LLM" in kinds          # a generation
    assert "TOOL" in kinds         # the Read call

    root = kinds["AGENT"][0]
    assert root.parent_span_id == b""
    assert len({s.trace_id for s in spans_out}) == 1     # one trace per session


def test_disabled_folder_posts_nothing(server, tmp_path, fixtures_dir):
    home = tmp_path / "home"
    (home / ".claude" / "rius").mkdir(parents=True)
    payload = {"session_id": "s2",
               "transcript_path": str(fixtures_dir / "simple.jsonl"),
               "cwd": "/somewhere/else", "hook_event_name": "PostToolUse"}
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin",
           "RIUS_API_KEY": "glassflow_testkey", "RIUS_ENDPOINT": server}
    subprocess.run([sys.executable, HOOK, "PostToolUse"],
                   input=json.dumps(payload), capture_output=True,
                   text=True, env=env, timeout=30)
    time.sleep(2)
    assert received == []
```

- [ ] **Step 2: Run it**

Run: `python3 -m pytest tests/test_e2e.py -q`
Expected: 2 passed. If `detached exporter never POSTed` fires, the bug is in
Task 8's `Popen` detachment, not in the exporter — check `start_new_session`.

- [ ] **Step 3: Run the whole suite**

Run: `python3 -m pytest -q`
Expected: everything passes.

- [ ] **Step 4: Commit**

```bash
git add tests/test_e2e.py
git commit -m "test: end-to-end hook to OTLP receiver"
```

---

### Task 11: README, install, and staging acceptance

**Files:**
- Create: `README.md`, `.env.example`

**Interfaces:**
- Consumes: everything.
- Produces: the shippable artifact.

- [ ] **Step 1: Write `.env.example`**

```
# Your Rius API key. Required -- without it nothing is traced.
RIUS_API_KEY=glassflow_...

# Defaults to the EU production ingest. Point it at staging for testing.
RIUS_ENDPOINT=https://ingest.staging.rius.glassflow.xyz
```

- [ ] **Step 2: Write `README.md`**

It must lead with, not bury, the content warning (spec §7): by default this
sends prompts, assistant messages, tool inputs and **tool outputs** to Rius —
which includes the contents of files Claude Code read and the output of commands
it ran. State that the default is off per folder, and that
`RIUS_CAPTURE_CONTENT=false` keeps structure while dropping content.

Cover: install (local path for development, GitHub for everyone else), enabling
a folder, `/rius` usage, the settings table from spec §8.3, what a trace looks
like, the generation-timing approximation (spec §4), and how to uninstall.

- [ ] **Step 3: Install the plugin locally and verify it loads**

```bash
# in Claude Code:
#   /plugin marketplace add /path/to/rius-coding-agents
#   /plugin install rius-claude-code@rius-coding-agents
#   /reload-plugins          (after any later edit -- there is no file watcher)
```

Expected: the plugin appears in `/plugin`, and `/rius status` runs and reports
**off**, naming the reason.

- [ ] **Step 4: Enable this folder and run a real session against staging**

```bash
echo 'RIUS_API_KEY=glassflow_...' > .env.local          # never committed
```

Put the key and `RIUS_ENDPOINT=https://ingest.staging.rius.glassflow.xyz` into
`~/.claude/settings.json` under `env`, then `/rius enable-here`, then run a
session that reads a file and runs a command.

- [ ] **Step 5: Verify in the Rius UI — the acceptance gate**

At `https://staging.rius.glassflow.xyz`, confirm all of:

1. A trace appears **while the session is still running**, not only at the end.
2. The waterfall nests: session root → turn → generation → tool.
3. Token counts on a generation match that assistant message's `usage` in the transcript.
4. Tool spans show the tool name, and a failed tool is red.
5. `session.id` matches the Claude Code session id.
6. Cost is non-zero — if it is zero, the model is unpriced in the Rius backend's pricing table (spec §13.3), which is a backend finding to report, not a plugin bug.

Report any of these that fail rather than working around them.

- [ ] **Step 6: Commit and push**

```bash
git add README.md .env.example
git commit -m "docs: README with content-capture warning and install guide"
git push -u origin main
```

---

## Self-Review

**Spec coverage.** §2 source of truth → Task 2. §3 span model, identifiers,
attributes → Task 3. §4 timing → Task 3 (generation start from `last_ns`).
§5 liveness and pending spans → Task 3 plus Task 7's event handling. §6.1
runtime → Tasks 7, 8. §6.2 zero dependencies → Global Constraints and Task 0's
`dependencies = []`. §6.3 protobuf → Tasks 1, 6. §7 content → Task 3
(`truncate`, `capture_content`) and Task 11's README. §8 config → Tasks 4, 9.
§9 failure handling → Tasks 5, 7, 8 (lock contention, corrupt state, missing
transcript, always exit 0). §10 testing → every task, plus Tasks 10, 11.
§11 layout → File Structure. §13.1 → resolved before this plan was written.
§13.2 and §13.3 → Task 11 Step 5.

**Gap found and closed:** the spec's §11 layout omitted `marketplace.json`,
without which the plugin cannot be installed at all. Added in Task 8.

**Type consistency.** `Span` is constructed with the same keyword arguments in
Tasks 3, 6 and 10. `state` keys are identical in Tasks 3, 5 and 7. `Ctx` fields
match between Tasks 3 and 7. `config.Config` fields match between Tasks 4, 7
and 9.

---

### Task 12: Heartbeat pinger

**Added 2026-09-22, after the plan was written**, at the user's request. Scope
addition, not a defect fix.

**Files:**
- Create: `scripts/heartbeat.py`
- Test: `tests/test_heartbeat.py`
- Modify: `scripts/hook.py` (spawn on `SessionStart`, stop on `SessionEnd`)

**Interfaces:**
- Consumes: `config` (Task 4), `state` (Task 5).
- Produces:
  - `build_payload(instance_id, agent_name, open_traces, stopped=False) -> dict`
  - `ping(endpoint, api_key, payload, timeout) -> bool`
  - `run(session_id, home, env, parent_pid) -> None` — the loop
  - `stop_path(session_id, home) -> str`, `pid_path(session_id, home) -> str`

**Why a separate process, when nothing else here has one.** Spans only export
when they finish, so an idle or wedged agent is indistinguishable from a healthy
quiet one — that gap is the whole reason the SDK has heartbeats. Emitting pings
opportunistically from the existing hooks was the obvious way to avoid a
background process, and it does not work: **no hook fires between `PreToolUse`
and `PostToolUse`**, so a tool call longer than 60s would cross the backend's
"gone" threshold and report the agent as dead precisely while it is working
hardest. A false "gone" is worse than no heartbeat at all.

The pinger is therefore deliberately minimal: it holds no span state, parses no
transcript, and if it dies you lose liveness only — never trace data. That is a
materially smaller blast radius than the per-session tailer daemon rejected in
spec §12.

**Contract — verified against `glassflow-python/src/rius/heartbeat.py` and
the Rius backend's heartbeat handler. Do not improvise these.**

- `POST <endpoint>/v1/heartbeat`, `Content-Type: application/json` — note this
  endpoint takes JSON, unlike `/v1/traces` which is protobuf-only.
- Payload v1, exactly these keys:
  ```json
  {"v": 1, "instance_id": "<uuid>", "agent_name": "<service name>",
   "sent_at": "2026-09-22T10:00:00.000Z", "sdk_language": "python",
   "sdk_version": "rius-claude-code/<version>",
   "open_traces": ["<trace id>"], "open_trace_count": 1}
  ```
  `sent_at` is RFC3339 UTC with millisecond precision and a `Z` suffix.
  `open_traces` is capped at 32 entries.
- `"stopped": true` appears ONLY on the final ping. `false` is never sent.
- Timings: interval **15s**; the backend marks an instance stale at **30s** and
  gone at **60s**. Ping timeout 3s; the final stopped ping gets 1s.
- **Never retry a failed ping.** Liveness is only true fresh — a late heartbeat
  is misinformation. Log once, drop it, continue the loop.
- `instance_id` MUST be the same value the exporter puts in `service.instance.id`
  on its spans (persisted in `state` by Task 7). That equality is the only thing
  joining heartbeats to traces in the backend; a fresh uuid here silently breaks
  it while appearing to work.

**Lifetime.** Started by `hook.py` on `SessionStart` only, and only when config
resolves enabled. It exits on the first of:
1. the stop file appearing (written by `hook.py` on `SessionEnd`) — sends the
   final `stopped: true` ping first;
2. the parent Claude Code PID no longer existing (`os.kill(ppid, 0)` raising) —
   the backstop for a killed terminal, which never runs `SessionEnd`;
3. a 12-hour absolute cap, so no bug can leave a pinger running forever.

A pid file prevents duplicates: if one exists and that process is alive, exit
immediately rather than starting a second pinger for one session.

- [ ] **Step 1: Write the failing test**

```python
import json
import os
import time

import pytest

import heartbeat


def test_payload_shape_matches_contract():
    p = heartbeat.build_payload("inst-1", "claude-code", ["abc"], stopped=False)
    assert p["v"] == 1
    assert p["instance_id"] == "inst-1"
    assert p["agent_name"] == "claude-code"
    assert p["sdk_language"] == "python"
    assert p["open_traces"] == ["abc"]
    assert p["open_trace_count"] == 1
    assert "stopped" not in p          # false is NEVER sent
    assert p["sent_at"].endswith("Z")
    assert p["sent_at"].count(".") == 1
    # RFC3339 UTC, millisecond precision
    assert len(p["sent_at"]) == len("2026-09-22T10:00:00.000Z")


def test_stopped_only_on_final_ping():
    p = heartbeat.build_payload("i", "a", [], stopped=True)
    assert p["stopped"] is True


def test_open_traces_capped_at_32():
    p = heartbeat.build_payload("i", "a", ["t%d" % i for i in range(100)])
    assert len(p["open_traces"]) == 32
    assert p["open_trace_count"] == 100     # the COUNT is not capped


def test_ping_never_raises_on_transport_failure(monkeypatch):
    def boom(*a, **k):
        raise OSError("network down")
    monkeypatch.setattr(heartbeat.urllib.request, "urlopen", boom)
    assert heartbeat.ping("http://127.0.0.1:1", "k", {"v": 1}, 0.1) is False


def test_ping_posts_json_to_heartbeat_path():
    sent = {}

    class FakeResp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False

    import urllib.request as u
    real = u.urlopen

    def fake(req, timeout=None):
        sent["url"] = req.full_url
        sent["ctype"] = req.headers.get("Content-type")
        sent["auth"] = req.headers.get("Authorization")
        sent["body"] = json.loads(req.data.decode())
        return FakeResp()

    u.urlopen = fake
    try:
        assert heartbeat.ping("https://ingest.test", "glassflow_k", {"v": 1}, 1.0) is True
    finally:
        u.urlopen = real
    assert sent["url"] == "https://ingest.test/v1/heartbeat"
    assert sent["ctype"] == "application/json"
    assert sent["auth"] == "Bearer glassflow_k"
    assert sent["body"] == {"v": 1}


def test_pid_file_prevents_a_second_pinger(tmp_path):
    home = str(tmp_path)
    os.makedirs(os.path.join(home, ".claude", "rius", "state"), exist_ok=True)
    with open(heartbeat.pid_path("s1", home), "w") as fh:
        fh.write(str(os.getpid()))          # a PID that is definitely alive
    assert heartbeat.already_running("s1", home) is True


def test_stale_pid_file_does_not_block(tmp_path):
    home = str(tmp_path)
    os.makedirs(os.path.join(home, ".claude", "rius", "state"), exist_ok=True)
    with open(heartbeat.pid_path("s1", home), "w") as fh:
        fh.write("999999")                  # almost certainly not a live PID
    assert heartbeat.already_running("s1", home) is False


def test_parent_death_is_detected():
    assert heartbeat.parent_alive(os.getpid()) is True
    assert heartbeat.parent_alive(999999) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_heartbeat.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'heartbeat'`

- [ ] **Step 3: Write `scripts/heartbeat.py`**

Implement to satisfy the tests and the contract above. The loop:

```
if already_running(session_id, home): return
write pid file
send first ping IMMEDIATELY (the agent appears without waiting an interval)
while True:
    sleep up to 15s, waking early to check the stop file
    if stop file exists: send stopped ping, clean up, return
    if not parent_alive(parent_pid): clean up WITHOUT a stopped ping, return
    if elapsed > 12h: clean up, return
    send ping
```

Exiting without a stopped ping on parent death is correct, not a gap — the
backend's stale→gone path exists for exactly that case, and claiming a clean
stop for a killed process would be a lie.

`instance_id` and the open trace id are read from the session state file written
by Task 7. If state has no instance id yet (the pinger started before the first
export), ping with the id it finds once it appears, and skip `open_traces` until
then — never invent one.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_heartbeat.py -q`
Expected: 8 passed.

- [ ] **Step 5: Wire into `scripts/hook.py`**

On `SessionStart` (after the config check passes), spawn `heartbeat.py` detached
exactly as the exporter is spawned, passing the session id and `os.getppid()`.
On `SessionEnd`, write the stop file before spawning the final exporter run.

Add to `tests/test_hook.py`:

```python
def test_session_end_writes_the_stop_file(tmp_path):
    # with tracing enabled for tmp_path, SessionEnd must create the stop marker
    # so a running pinger sends its final stopped ping and exits.
    ...
```

Fill this in against the real `hook.py` structure — enable a folder via a path
rule in a temp HOME, run the hook with `SessionEnd`, and assert the stop file
exists afterwards.

- [ ] **Step 6: Run the whole suite**

Run: `.venv/bin/python -m pytest -q`
Expected: everything passes.

- [ ] **Step 7: Commit**

```bash
git add scripts/heartbeat.py scripts/hook.py tests/test_heartbeat.py tests/test_hook.py
git commit -m "feat: heartbeat pinger for agent liveness"
```
