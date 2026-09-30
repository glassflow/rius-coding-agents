"""A conversation Claude Code moves to a new session id is traced once.

When Claude Code 2.1.x sends a session to the background, its daemon starts
the conversation under a NEW session id: it appends a `continued-in` record
to the old transcript, naming the new id, and writes a new transcript that
opens with a copy of the whole history -- the same entry uuids, a new
sessionId and new promptIds. The old process gets no SessionEnd.

Read naively, that is two traces of the same work: the new session re-sent
every copied entry under its own trace (both traces started at the same
millisecond, and every token before the switch counted twice), and the old
trace stayed open forever.

Now the new session skips the entries the old one already sent, links to it
with `cc.continued_from`, and closes the old trace at the moment of the
switch.
"""
import json

import pytest

import exporter
from rius_cc import config, spans, state

OLD = "aaaaaaaa-0000-0000-0000-000000000001"
NEW = "bbbbbbbb-0000-0000-0000-000000000002"
ENV = {"RIUS_API_KEY": "glassflow_k", "RIUS_ENDPOINT": "https://ingest.test"}
SWITCH_TS = "2026-09-30T10:05:00.000Z"


def _row(sid, **kw):
    row = {"isSidechain": False, "sessionId": sid, "cwd": "/tmp/proj",
           "gitBranch": "main", "version": "2.1.284"}
    row.update(kw)
    return row


def _history(sid, prompt_id):
    """The conversation before the switch, as the session `sid` writes it."""
    return [
        _row(sid, type="user", uuid="h1", parentUuid=None, promptId=prompt_id,
             timestamp="2026-09-30T10:00:00.000Z",
             message={"role": "user", "content": "build the thing"}),
        _row(sid, type="assistant", uuid="h2", parentUuid="h1",
             timestamp="2026-09-30T10:00:02.000Z",
             message={"id": "msg_before", "role": "assistant",
                      "model": "claude-opus-5", "stop_reason": "tool_use",
                      "content": [{"type": "tool_use", "id": "toolu_before",
                                   "name": "Bash",
                                   "input": {"command": "make build"}}],
                      "usage": {"input_tokens": 2, "output_tokens": 300}}),
        _row(sid, type="user", uuid="h3", parentUuid="h2", promptId=prompt_id,
             timestamp="2026-09-30T10:00:05.000Z",
             message={"role": "user", "content": [
                 {"type": "tool_result", "tool_use_id": "toolu_before",
                  "is_error": False, "content": "built"}]}),
        _row(sid, type="assistant", uuid="h4", parentUuid="h3",
             timestamp="2026-09-30T10:00:07.000Z",
             message={"id": "msg_done", "role": "assistant",
                      "model": "claude-opus-5", "stop_reason": "end_turn",
                      "content": [{"type": "text", "text": "It builds."}],
                      "usage": {"input_tokens": 2, "output_tokens": 40}}),
    ]


def _after_switch():
    return [
        _row(NEW, type="user", uuid="n1", parentUuid="h4", promptId="p-new",
             timestamp="2026-09-30T10:06:00.000Z",
             message={"role": "user", "content": "now test it"}),
        _row(NEW, type="assistant", uuid="n2", parentUuid="n1",
             timestamp="2026-09-30T10:06:03.000Z",
             message={"id": "msg_after", "role": "assistant",
                      "model": "claude-opus-5", "stop_reason": "end_turn",
                      "content": [{"type": "text", "text": "Tests pass."}],
                      "usage": {"input_tokens": 2, "output_tokens": 25}}),
    ]


def _write(path, rows):
    with open(path, "a") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    (h / ".claude" / "rius").mkdir(parents=True)
    with open(config.path_rules_path(str(h)), "w") as fh:
        json.dump({"enabled_paths": ["/tmp"]}, fh)
    return str(h)


@pytest.fixture
def sent(monkeypatch):
    """Every span handed to the encoder, in order."""
    out = []

    def encode(resource, span_list):
        out.extend(span_list)
        return b"x"

    monkeypatch.setattr(exporter.otlp, "encode", encode)
    monkeypatch.setattr(exporter.otlp, "export",
                        lambda ep, key, body, timeout=5.0: 200)
    return out


def _hook(home, sid, path, event):
    return exporter.run(event, {"session_id": sid, "cwd": "/tmp/proj",
                                "transcript_path": str(path),
                                "hook_event_name": event}, ENV, home)


def _switch(tmp_path, home, continued_in=True):
    proj = tmp_path / "proj"
    proj.mkdir()
    old, new = proj / (OLD + ".jsonl"), proj / (NEW + ".jsonl")
    _write(old, _history(OLD, "p-old"))
    _hook(home, OLD, old, "PostToolUse")
    _hook(home, OLD, old, "Stop")
    if continued_in:
        _write(old, [{"type": "continued-in", "timestamp": SWITCH_TS,
                      "sessionId": OLD, "continuedInSessionId": NEW}])
    # The new transcript: bookkeeping lines, the copied history, new work.
    _write(new, [{"type": "ai-title", "sessionId": NEW, "aiTitle": "t"},
                 {"type": "mode", "sessionId": NEW, "mode": "normal"}])
    _write(new, [dict(r, promptId="p-copy") if r.get("promptId") else r
                 for r in _history(NEW, "p-copy")])
    return old, new


def _trace(spans_sent, sid):
    tid = spans.trace_id_for(sid)
    latest = {}
    for s in spans_sent:
        if s.trace_id != tid:
            continue
        if s.pending and s.span_id in latest:
            continue
        latest[s.span_id] = s
    return latest


def _llm_outputs(trace):
    return sorted(s.attributes["gen_ai.usage.output_tokens"]
                  for s in trace.values() if s.kind_oi == "LLM")


def test_copied_history_is_not_sent_again(tmp_path, home, sent):
    _, new = _switch(tmp_path, home)
    _hook(home, NEW, new, "SessionStart")
    _write(new, _after_switch())
    _hook(home, NEW, new, "Stop")
    assert _llm_outputs(_trace(sent, OLD)) == [40, 300]
    new_trace = _trace(sent, NEW)
    assert _llm_outputs(new_trace) == [25]
    assert spans.span_id_for("toolu_before") not in new_trace


def test_the_new_trace_starts_where_the_new_work_does(tmp_path, home, sent):
    _, new = _switch(tmp_path, home)
    _write(new, _after_switch())
    _hook(home, NEW, new, "Stop")
    root = _trace(sent, NEW)[spans.span_id_for("session:" + NEW)]
    assert root.start_ns == exporter.transcript._timestamp_ns(
        "2026-09-30T10:06:00.000Z")
    assert root.attributes["cc.continued_from"] == OLD


def test_the_old_trace_is_closed_at_the_switch(tmp_path, home, sent):
    _, new = _switch(tmp_path, home)
    _hook(home, NEW, new, "SessionStart")
    root = _trace(sent, OLD)[spans.span_id_for("session:" + OLD)]
    assert root.pending is False
    assert root.end_ns == exporter.transcript._timestamp_ns(SWITCH_TS)
    assert state.load(OLD, home).get("finalized") is True
    assert not any(s.pending for s in _trace(sent, OLD).values())


def test_an_old_session_that_already_ended_is_left_alone(tmp_path, home, sent):
    old, new = _switch(tmp_path, home)
    _hook(home, OLD, old, "SessionEnd")
    before = len(_trace(sent, OLD))
    ended = [s for s in sent if s.trace_id == spans.trace_id_for(OLD)]
    _hook(home, NEW, new, "SessionStart")
    assert [s for s in sent if s.trace_id == spans.trace_id_for(OLD)] == ended
    assert len(_trace(sent, OLD)) == before


def test_no_continued_in_record_means_no_skipping(tmp_path, home, sent):
    """Skipping is decided by Claude Code's own link, never guessed from
    matching uuids alone."""
    _, new = _switch(tmp_path, home, continued_in=False)
    _hook(home, NEW, new, "Stop")
    assert _llm_outputs(_trace(sent, NEW)) == [40, 300]
    assert "cc.continued_from" not in \
        _trace(sent, NEW)[spans.span_id_for("session:" + NEW)].attributes


def test_a_stopped_old_session_is_closed_without_content(tmp_path, home, sent):
    """#7's rule holds here too: an old session whose folder was disabled
    mid-session is closed with capture off."""
    _, new = _switch(tmp_path, home)
    st = state.load(OLD, home)
    st["content_stopped"] = True
    st["open_turns"]["p-old"] = {"span_id": spans.span_id_for("turn:p-old"),
                                 "parent_span_id": spans.span_id_for("session:" + OLD),
                                 "start_ns": 1, "source": "user", "text": ""}
    state.save(OLD, home, st)
    del sent[:]
    _hook(home, NEW, new, "SessionStart")
    closing = [s for s in sent if s.trace_id == spans.trace_id_for(OLD)]
    assert closing, "the old trace was not closed"
    for s in closing:
        assert "input.value" not in s.attributes
        assert "output.value" not in s.attributes
