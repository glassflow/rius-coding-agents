"""A session that started traced must still close on SessionEnd after its
folder is disabled mid-session.

The backend counts a trace on the Agents/Users tabs only when EVERY span of
it has a finished row (min(Finished) over the trace), so one span left
pending hides the whole session. Disabling must still beat enabling for
content: nothing that happens after the disable is exported, and the closing
spans carry no content.
"""
import json
import os
import pathlib

import pytest

import exporter
from rius_cc import config, spans, state

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
SID = "22222222-2222-2222-2222-222222222222"
ENV = {"RIUS_API_KEY": "glassflow_k", "RIUS_ENDPOINT": "https://ingest.test"}
CONTENT_KEYS = ("input.value", "output.value")
LATER_PROMPT = {
    "parentUuid": "u2", "isSidechain": False, "type": "user", "uuid": "u9",
    "timestamp": "2026-09-22T10:05:00.000Z", "sessionId": SID, "promptId": "p9",
    "cwd": "/tmp/proj", "gitBranch": "main", "version": "2.1.278",
    "message": {"role": "user", "content": [
        {"type": "text", "text": "typed after the disable"}]},
}


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    (h / ".claude" / "rius").mkdir(parents=True)
    _rules(str(h), enabled=["/tmp"])
    return str(h)


@pytest.fixture
def sent(monkeypatch):
    """Every Span handed to the wire, batch by batch."""
    batches = []
    monkeypatch.setattr(exporter.otlp, "encode",
                        lambda resource, out: batches.append(list(out)) or b"x")
    monkeypatch.setattr(exporter.otlp, "export",
                        lambda ep, key, body, timeout=5.0: 200)
    return batches


def _rules(home, enabled=(), disabled=()):
    with open(config.path_rules_path(home), "w") as fh:
        json.dump({"enabled_paths": list(enabled),
                   "disabled_paths": list(disabled)}, fh)


def _disable(home):
    _rules(home, enabled=["/tmp"], disabled=["/tmp/proj"])


def _transcript(tmp_path, n_lines):
    """The tool_call fixture cut after `n_lines`: 2 leaves the turn AND the
    tool open, as in a session that dies (or is disabled) mid-tool."""
    lines = (FIXTURES / "tool_call.jsonl").read_text().splitlines()[:n_lines]
    path = tmp_path / (SID + ".jsonl")
    path.write_text("\n".join(lines) + "\n")
    return path


def _append(path, *entries):
    with open(path, "a") as fh:
        for entry in entries:
            fh.write((entry if isinstance(entry, str) else json.dumps(entry)) + "\n")


def _run(event, path, home, env=ENV):
    payload = {"session_id": SID, "transcript_path": str(path),
               "cwd": "/tmp/proj", "hook_event_name": event}
    return exporter.run(event, payload, env, home)


def _all(batches):
    return [s for batch in batches for s in batch]


def _left_pending(batches):
    """Span ids sent pending and never sent finished afterwards."""
    last = {}
    for span in _all(batches):
        last[span.span_id] = span.pending
    return sorted(sid for sid, pending in last.items() if pending)


# --- the bug -----------------------------------------------------------------

def test_a_session_disabled_mid_way_still_closes_every_span(home, sent, tmp_path):
    path = _transcript(tmp_path, 2)
    _run("PostToolUse", path, home)
    assert _left_pending(sent), "fixture should leave spans open"

    _disable(home)
    _run("SessionEnd", path, home)

    assert _left_pending(sent) == []
    root = spans.span_id_for("session:" + SID)
    assert [s for s in sent[-1] if s.span_id == root and not s.pending]


def test_nothing_after_the_disable_is_exported(home, sent, tmp_path):
    path = _transcript(tmp_path, 2)
    _run("PostToolUse", path, home)
    _disable(home)
    before = len(sent)

    _append(path, (FIXTURES / "tool_call.jsonl").read_text().splitlines()[2],
            LATER_PROMPT)
    _run("PostToolUse", path, home)
    _run("Stop", path, home)
    assert len(sent) == before, "a disabled folder exported mid-session"

    _run("SessionEnd", path, home)
    closing = sent[-1]
    assert spans.span_id_for("turn:p9") not in {s.span_id for s in closing}
    for span in closing:
        for key in CONTENT_KEYS:
            assert key not in span.attributes, (span.name, key)
        assert span.status_message == ""


def test_the_closing_spans_carry_no_content_even_with_capture_on(home, sent,
                                                                tmp_path):
    path = _transcript(tmp_path, 2)
    _run("PostToolUse", path, home)
    assert state.load(SID, home)["open_turns"]["p1"]["text"] == "read a file"

    _disable(home)
    _run("SessionEnd", path, home)
    assert not any(key in span.attributes
                   for span in sent[-1] for key in CONTENT_KEYS)


def test_re_enabling_does_not_resume_a_stopped_session(home, sent, tmp_path):
    path = _transcript(tmp_path, 2)
    _run("PostToolUse", path, home)
    _disable(home)
    _run("PostToolUse", path, home)       # the exporter sees the disable
    _rules(home, enabled=["/tmp"])        # ... and it is undone
    before = len(sent)

    _append(path, LATER_PROMPT)
    _run("PostToolUse", path, home)
    assert len(sent) == before

    _run("SessionEnd", path, home)
    assert _left_pending(sent) == []
    assert spans.span_id_for("turn:p9") not in {s.span_id for s in _all(sent)}


def test_a_session_never_traced_stays_silent(home, sent, tmp_path):
    path = _transcript(tmp_path, 2)
    _disable(home)
    _run("PostToolUse", path, home)
    _run("SessionEnd", path, home)
    assert sent == []
    assert not state.load(SID, home).get("content_stopped")


def test_without_a_key_nothing_is_sent_and_nothing_breaks(home, sent, tmp_path):
    path = _transcript(tmp_path, 2)
    _run("PostToolUse", path, home)
    _disable(home)
    before = len(sent)
    assert _run("SessionEnd", path, home, env={}) == 0
    assert len(sent) == before


# --- the same invariant for a session that stays enabled ----------------------

def test_session_end_closes_a_tool_that_never_returned(home, sent, tmp_path):
    path = _transcript(tmp_path, 2)
    _run("PostToolUse", path, home)
    _run("SessionEnd", path, home)
    assert _left_pending(sent) == []
    assert state.load(SID, home)["open_tools"] == {}


def test_new_activity_after_a_session_end_reopens_the_session(home, sent,
                                                            tmp_path):
    path = _transcript(tmp_path, 3)
    _run("PostToolUse", path, home)
    _run("SessionEnd", path, home)
    assert state.load(SID, home)["finalized"] is True

    _append(path, LATER_PROMPT)            # a resumed session carries on
    _run("PostToolUse", path, home)
    assert state.load(SID, home)["finalized"] is False


def test_a_stopped_session_never_moves_its_transcript_offset(home, sent,
                                                             tmp_path):
    # The offset staying put is why the stop has to be sticky; it must not
    # quietly advance, or a later change to the stickiness would look safe.
    path = _transcript(tmp_path, 2)
    _run("PostToolUse", path, home)
    offset = state.load(SID, home)["offset"]
    _disable(home)
    _append(path, LATER_PROMPT)
    for event in ("PostToolUse", "Stop", "SessionEnd"):
        _run(event, path, home)
        assert state.load(SID, home)["offset"] == offset, event
