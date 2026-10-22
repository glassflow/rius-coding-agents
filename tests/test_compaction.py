"""Every compaction is a span, and the call after it says it ran on one.

Claude Code writes a `compact_boundary` system entry (the trigger, the sizes
before and after, how long it took) and then a user entry holding the summary
text, flagged `isCompactSummary`. The summary is stamped slightly BEFORE the
boundary and may be read in a later hook than it. The boundary becomes a
CHAIN span with gen_ai.operation.name "compaction"; the summary is never a
turn. tests/fixtures/compaction.jsonl has an automatic and a manual
compaction, each followed by two model calls.
"""
import json

import pytest

import exporter
from rius_cc import config, spans, state, transcript
from rius_cc import _semconv_generated as semconv
from tests.signed_in import sign_in

FIXTURE = "compaction.jsonl"
SESSION = "99999999-9999-9999-9999-999999999999"
T0 = 1790762400000000000          # 2026-09-30T10:00:00Z
SUMMARY_1 = "Summary of the first stretch: the user asked for a greeting."
SUMMARY_2 = "Summary of the second stretch: a café menu was drafted."


def _ctx(capture=True, max_attr_bytes=32768):
    return spans.Ctx(session_id=SESSION, cwd="/tmp/proj", git_branch="main",
                     cc_version="2.1.304", service_name="claude-code",
                     capture_content=capture, max_attr_bytes=max_attr_bytes)


def _build(entries, st=None, capture=True, max_attr_bytes=32768):
    st = st if st is not None else state.new_state()
    return spans.build(entries, st, _ctx(capture, max_attr_bytes)), st


def _entries(fixtures_dir):
    return transcript.read_from(str(fixtures_dir / FIXTURE), 0)[0]


def _compactions(out):
    return [s for s in out if s.attributes.get("gen_ai.operation.name") == "compaction"]


def _llm(out):
    """The latest copy of each LLM span, by response."""
    return {s.attributes["gen_ai.usage.output_tokens"]: s
            for s in out if s.kind_oi == "LLM"}


def _boundary_line(uuid="b1", ts="2026-09-30T10:10:30.000Z", **meta):
    return {"sessionId": SESSION, "cwd": "/tmp/proj", "gitBranch": "main",
            "version": "2.1.304", "isSidechain": False, "parentUuid": None,
            "type": "system", "subtype": "compact_boundary", "uuid": uuid,
            "timestamp": ts, "compactMetadata": meta}


def _summary_line(text, uuid="s1", ts="2026-09-30T10:10:29.500Z", pid="ps1"):
    return {"sessionId": SESSION, "cwd": "/tmp/proj", "gitBranch": "main",
            "version": "2.1.304", "isSidechain": False, "parentUuid": "b1",
            "type": "user", "uuid": uuid, "timestamp": ts, "promptId": pid,
            "isCompactSummary": True,
            "message": {"role": "user", "content": text}}


def _assistant_line(uuid, ts, msg_id, out_tokens):
    return {"sessionId": SESSION, "cwd": "/tmp/proj", "gitBranch": "main",
            "version": "2.1.304", "isSidechain": False, "parentUuid": None,
            "type": "assistant", "uuid": uuid, "timestamp": ts,
            "message": {"id": msg_id, "type": "message", "role": "assistant",
                        "model": "claude-opus-5", "stop_reason": "end_turn",
                        "content": [{"type": "text", "text": "ok"}],
                        "usage": {"input_tokens": 1, "output_tokens": out_tokens}}}


def _parse(*lines):
    entries = []
    for raw in lines:
        entry, reason = transcript.parse_line_ex(json.dumps(raw))
        assert reason is None
        entries.append(entry)
    return entries


def test_each_compaction_is_a_span_with_its_numbers(fixtures_dir):
    out, _ = _build(_entries(fixtures_dir), capture=False)
    auto, manual = _compactions(out)

    assert auto.attributes == {
        "openinference.span.kind": "CHAIN",
        "session.id": SESSION,
        "gen_ai.operation.name": "compaction",
        "rius.compaction.kind": "summarize",
        "rius.compaction.executor": "client",
        "rius.compaction.trigger": "auto",
        "rius.compaction.tokens.before": 150000,
        "rius.compaction.tokens.after": 20000,
        "rius.compaction.tokens.dropped": 130000,
        "rius.compaction.messages.kept": 3,
        "rius.compaction.id": "b1",
        "rius.compaction.summary.bytes": len(SUMMARY_1.encode("utf-8")),
    }
    assert manual.attributes == {
        "openinference.span.kind": "CHAIN",
        "session.id": SESSION,
        "gen_ai.operation.name": "compaction",
        "rius.compaction.kind": "summarize",
        "rius.compaction.executor": "client",
        "rius.compaction.trigger": "manual",
        "rius.compaction.tokens.before": 90000,
        "rius.compaction.tokens.after": 15000,
        "rius.compaction.tokens.dropped": 75000,
        "rius.compaction.messages.kept": 1,
        "rius.compaction.id": "b2",
        # bytes, not characters: the e-acute is two
        "rius.compaction.summary.bytes": len(SUMMARY_2.encode("utf-8")),
    }
    assert len(SUMMARY_2.encode("utf-8")) == len(SUMMARY_2) + 1
    assert auto.name == manual.name == "compaction"
    assert auto.pending is manual.pending is False
    assert auto.status_code == "OK"


def test_a_compaction_ends_at_its_boundary_and_started_duration_ms_before(fixtures_dir):
    out, _ = _build(_entries(fixtures_dir))
    auto, manual = _compactions(out)
    assert auto.end_ns == T0 + 630 * 1_000_000_000          # 10:10:30
    assert auto.start_ns == auto.end_ns - 30_000 * 1_000_000
    assert manual.end_ns == T0 + 1220 * 1_000_000_000       # 10:20:20
    assert manual.start_ns == manual.end_ns - 20_000 * 1_000_000


def test_a_compaction_hangs_off_the_open_turn(fixtures_dir):
    out, st = _build(_entries(fixtures_dir))
    auto, manual = _compactions(out)
    assert auto.parent_span_id == spans.span_id_for("turn:p1")
    assert manual.parent_span_id == spans.span_id_for("turn:p2")
    assert auto.span_id == spans.span_id_for("compaction:b1")
    assert manual.span_id == spans.span_id_for("compaction:b2")

    # With no turn open, the root.
    fresh, _ = _build(_parse(_boundary_line(trigger="auto", preTokens=5,
                                            postTokens=1, durationMs=1)))
    (only,) = _compactions(fresh)
    assert only.parent_span_id == spans.span_id_for("session:" + SESSION)


def test_only_the_first_call_after_a_compaction_is_flagged(fixtures_dir):
    out, _ = _build(_entries(fixtures_dir))
    calls = _llm(out)
    key = "gen_ai.conversation.compacted"
    assert calls[5].attributes.get(key) is None             # before any
    assert calls[6].attributes[key] is True                 # first after #1
    assert calls[7].attributes.get(key) is None
    assert calls[8].attributes[key] is True                 # first after #2
    assert calls[9].attributes.get(key) is None
    assert key not in [s for s in _compactions(out)][0].attributes


def test_the_flag_survives_the_hooks_a_response_arrives_in():
    """Boundary and summary in one hook, the call's lines in the next two."""
    st = state.new_state()
    _build(_parse(_boundary_line(trigger="auto", preTokens=5, postTokens=1,
                                 durationMs=1),
                  _summary_line("sum")), st)
    first = _parse(_assistant_line("a1", "2026-09-30T10:10:31.000Z", "msg_1", 3))
    out, _ = _build(first, st)
    assert _llm(out)[3].attributes["gen_ai.conversation.compacted"] is True
    # The same response, one line later, in another hook.
    more = _parse(_assistant_line("a2", "2026-09-30T10:10:32.000Z", "msg_1", 3))
    out, _ = _build(more, st)
    assert _llm(out)[3].attributes["gen_ai.conversation.compacted"] is True
    # The next response is not.
    nxt = _parse(_assistant_line("a3", "2026-09-30T10:10:33.000Z", "msg_2", 4))
    out, _ = _build(nxt, st)
    assert "gen_ai.conversation.compacted" not in _llm(out)[4].attributes


def test_reading_a_boundary_again_flags_no_later_call():
    """A copied history or a rewound offset shows the same boundaries twice."""
    st = state.new_state()
    flag = "gen_ai.conversation.compacted"
    first = _parse(_boundary_line(trigger="auto", preTokens=5, postTokens=1,
                                  durationMs=1),
                   _summary_line("sum"))
    second = _parse(_boundary_line(uuid="b2", ts="2026-09-30T10:20:30.000Z",
                                   trigger="manual", preTokens=9, postTokens=2,
                                   durationMs=1),
                    _summary_line("sum2", uuid="s2", ts="2026-09-30T10:20:29.500Z"))
    _build(first, st)
    out, _ = _build(_parse(_assistant_line("a1", "2026-09-30T10:10:31.000Z",
                                           "msg_1", 3)), st)
    assert _llm(out)[3].attributes[flag] is True
    _build(second, st)
    out, _ = _build(_parse(_assistant_line("a2", "2026-09-30T10:20:31.000Z",
                                           "msg_2", 4)), st)
    assert _llm(out)[4].attributes[flag] is True
    # The whole history comes round again: both boundaries are old news.
    out, _ = _build(first + second, st)
    assert _compactions(out) == []
    out, _ = _build(_parse(_assistant_line("a3", "2026-09-30T10:20:33.000Z",
                                           "msg_3", 5)), st)
    assert flag not in _llm(out)[5].attributes


def test_replaying_the_transcript_gives_the_same_span_ids(fixtures_dir):
    first, _ = _build(_entries(fixtures_dir))
    again, _ = _build(_entries(fixtures_dir))
    assert ([(s.span_id, s.start_ns, s.end_ns) for s in _compactions(first)]
            == [(s.span_id, s.start_ns, s.end_ns) for s in _compactions(again)])
    assert [s.span_id for s in first] == [s.span_id for s in again]
    assert len({s.span_id for s in _compactions(first)}) == 2


def test_the_summary_entry_is_not_a_turn(fixtures_dir):
    out, st = _build(_entries(fixtures_dir))
    # ps1 is a promptId only the summary carries; p2 is shared with a prompt.
    assert list(st["open_turns"]) == ["p1", "p2"]
    turns = [s for s in out if s.name == "turn"]
    assert {s.span_id for s in turns} == {spans.span_id_for("turn:p1"),
                                          spans.span_id_for("turn:p2")}
    assert spans.span_id_for("turn:ps1") not in {s.span_id for s in out}

    # ... and with nothing open, it still opens none.
    only, st2 = _build(_parse(_boundary_line(trigger="auto"),
                              _summary_line("sum", pid="ps9")))
    assert st2["open_turns"] == {}
    assert [s for s in only if s.name == "turn"] == []


def test_the_summary_still_restarts_the_context_account(fixtures_dir):
    out, _ = _build(_entries(fixtures_dir))
    sizes = json.loads(_llm(out)[6].attributes["rius.context.sizes"])
    assert sizes["input_messages"] == [
        {"role": "user",
         "parts": [{"type": "text", "bytes": len(SUMMARY_1.encode("utf-8"))}]}]
    assert "folded" not in sizes


def test_the_summary_text_is_content_and_follows_the_capture_gate(fixtures_dir):
    off, _ = _build(_entries(fixtures_dir), capture=False)
    for span in _compactions(off):
        assert "rius.compaction.summary.text" not in span.attributes
        assert "rius.compaction.summary.bytes" in span.attributes

    on, _ = _build(_entries(fixtures_dir), capture=True)
    auto, manual = _compactions(on)
    assert auto.attributes["rius.compaction.summary.text"] == SUMMARY_1
    assert manual.attributes["rius.compaction.summary.text"] == SUMMARY_2
    assert auto.attributes["rius.compaction.summary.bytes"] == len(SUMMARY_1)
    assert "rius.compaction.summary.text" in semconv.CONTENT_ATTRIBUTES
    assert "rius.compaction.summary.bytes" not in semconv.CONTENT_ATTRIBUTES


def test_the_summary_text_is_capped_and_scrubbed_like_other_content():
    secret = "AKIA" + "ABCDEFGHIJKLMNOP"
    text = "keys: %s then a long tail %s" % (secret, "x" * 200)
    out, _ = _build(_parse(_boundary_line(trigger="auto"),
                           _summary_line(text)), max_attr_bytes=40)
    (span,) = _compactions(out)
    shown = span.attributes["rius.compaction.summary.text"]
    assert secret not in shown
    assert "…[truncated" in shown
    assert len(shown.encode("utf-8")) < len(text.encode("utf-8"))
    # The size is the whole summary's, not what was kept.
    assert span.attributes["rius.compaction.summary.bytes"] == len(text.encode("utf-8"))

    # Under the cap it is whole.
    out, _ = _build(_parse(_boundary_line(trigger="auto"),
                           _summary_line("short")), max_attr_bytes=40)
    assert _compactions(out)[0].attributes["rius.compaction.summary.text"] == "short"


def test_a_summary_read_in_a_later_hook_updates_the_same_span():
    """The boundary is sent at once; the summary arrives in another hook."""
    boundary = _parse(_boundary_line(trigger="auto", preTokens=900,
                                     postTokens=100, durationMs=2000))
    summary = _parse(_summary_line("what was said"))
    st = state.new_state()

    out1, _ = _build(boundary, st)
    (first,) = _compactions(out1)
    assert "rius.compaction.summary.bytes" not in first.attributes
    assert "rius.compaction.summary.text" not in first.attributes
    assert st["open_compaction"] is not None
    # What waits in the state is sizes and ids, never the text to come.
    assert "what was said" not in json.dumps(st)

    out2, _ = _build(summary, st)
    (second,) = _compactions(out2)
    assert second.span_id == first.span_id
    assert (second.start_ns, second.end_ns) == (first.start_ns, first.end_ns)
    assert second.parent_span_id == first.parent_span_id
    assert second.attributes["rius.compaction.summary.bytes"] == len("what was said")
    assert second.attributes["rius.compaction.summary.text"] == "what was said"
    assert {k: v for k, v in second.attributes.items()
            if not k.startswith("rius.compaction.summary")} == first.attributes
    assert st["open_compaction"] is None
    assert "what was said" not in json.dumps(st)

    # Nothing left to say: a later hook sends no compaction span again.
    out3, _ = _build(_parse(_assistant_line("a1", "2026-09-30T10:11:00.000Z",
                                            "msg_1", 3)), st)
    assert _compactions(out3) == []


def test_state_that_survives_a_json_round_trip_still_gets_its_summary():
    st = state.new_state()
    _build(_parse(_boundary_line(trigger="manual", preTokens=9, postTokens=3,
                                 durationMs=500)), st)
    st = json.loads(json.dumps(st))
    out, _ = _build(_parse(_summary_line("abc")), st)
    (span,) = _compactions(out)
    assert span.attributes["rius.compaction.summary.bytes"] == 3
    assert span.attributes["rius.compaction.trigger"] == "manual"


def test_a_boundary_with_no_summary_is_exported_without_summary_attributes():
    out, st = _build(_parse(
        _boundary_line(trigger="auto", preTokens=50, postTokens=10, durationMs=0),
        _assistant_line("a1", "2026-09-30T10:10:31.000Z", "msg_1", 3)))
    (span,) = _compactions(out)
    assert span.attributes["rius.compaction.tokens.dropped"] == 40
    assert not [k for k in span.attributes if k.startswith("rius.compaction.summary")]
    assert _llm(out)[3].attributes["gen_ai.conversation.compacted"] is True


def test_a_summary_with_no_boundary_exports_nothing_and_is_no_turn():
    out, st = _build(_parse(_summary_line("orphan")))
    assert _compactions(out) == []
    assert [s for s in out if s.name == "turn"] == []
    assert st["open_turns"] == {}


@pytest.mark.parametrize("meta, expected_absent", [
    ({}, ["trigger", "tokens.before", "tokens.after", "tokens.dropped",
          "messages.kept"]),
    ({"trigger": "scheduled", "preTokens": "150000", "postTokens": 2.5},
     ["trigger", "tokens.before", "tokens.after", "tokens.dropped"]),
    ({"preTokens": -5, "postTokens": -1},
     ["tokens.before", "tokens.after", "tokens.dropped"]),
    ({"preTokens": True, "postTokens": None},
     ["tokens.before", "tokens.after", "tokens.dropped"]),
    ({"preTokens": 1000}, ["tokens.after", "tokens.dropped"]),
    ({"postTokens": 1000}, ["tokens.before", "tokens.dropped"]),
    # after bigger than before: both are kept, the "dropped" is not made up
    ({"preTokens": 10, "postTokens": 20}, ["tokens.dropped"]),
    ({"preservedMessages": "all"}, ["messages.kept"]),
    ({"preservedMessages": {"uuids": "k1"}}, ["messages.kept"]),
    ({"preservedMessages": {"allUuids": ["k1"]}}, ["messages.kept"]),
])
def test_malformed_metadata_never_writes_garbage(meta, expected_absent):
    out, _ = _build(_parse(_boundary_line(durationMs=1000, **meta),
                           _summary_line("s")))
    (span,) = _compactions(out)
    for name in expected_absent:
        assert "rius.compaction." + name not in span.attributes
    for key, value in span.attributes.items():
        if key.startswith("rius.compaction.tokens."):
            assert isinstance(value, int) and not isinstance(value, bool)
            assert value >= 0
    # What is always known is always there.
    assert span.attributes["rius.compaction.kind"] == "summarize"
    assert span.attributes["rius.compaction.executor"] == "client"
    assert span.attributes["rius.compaction.id"] == "b1"


def test_a_boundary_with_no_metadata_or_a_bad_duration_does_not_crash():
    raw = _boundary_line()
    del raw["compactMetadata"]
    out, _ = _build(_parse(raw))
    (span,) = _compactions(out)
    assert span.start_ns == span.end_ns

    for bad in ("30s", -5, None, 1.5, True):
        out, _ = _build(_parse(_boundary_line(durationMs=bad, trigger="auto")))
        (span,) = _compactions(out)
        assert span.start_ns == span.end_ns, bad
    garbage = _boundary_line()
    garbage["compactMetadata"] = "not a dict"
    (span,) = _compactions(_build(_parse(garbage))[0])
    assert span.attributes["rius.compaction.kind"] == "summarize"


def test_a_compaction_in_a_subagent_hangs_off_the_agent_span():
    scope = spans.new_scope()
    agent_span = spans.span_id_for("subagent:a1")
    out = spans.emit_entries(
        _parse(_boundary_line(trigger="auto", preTokens=5, postTokens=1,
                              durationMs=10),
               _summary_line("s"),
               _assistant_line("x1", "2026-09-30T10:10:31.000Z", "msg_1", 3)),
        scope, _ctx(), spans.trace_id_for(SESSION), agent_span, {},
        depth=1, key_prefix="agent:a1:", make_turns=False,
        inline_sidechains=False)
    (span,) = _compactions(out)
    assert span.parent_span_id == agent_span
    assert span.span_id == spans.span_id_for("agent:a1:compaction:b1")
    assert span.span_id != spans.span_id_for("compaction:b1")
    assert _llm(out)[3].attributes["gen_ai.conversation.compacted"] is True


def test_the_boundary_and_summary_leave_the_other_lines_alone(fixtures_dir):
    """Turn count, calls and their order are what they were without them."""
    entries = _entries(fixtures_dir)
    plain = [e for e in entries
             if not (e.is_compact_boundary() or e.is_compact_summary())]
    with_out, _ = _build(entries)
    plain_out, _ = _build(plain)
    kept = lambda out: [(s.span_id, s.kind_oi, s.name) for s in out
                        if s.attributes.get("gen_ai.operation.name") != "compaction"]
    assert kept(with_out) == kept(plain_out)
    # the one clock that must not run backwards: the summary is stamped
    # before the boundary, and the call after both starts at the boundary
    boundary_end = _compactions(with_out)[0].end_ns
    assert _llm(with_out)[6].start_ns == boundary_end



def _otlp_spans(body):
    from tests import otlp_json     # skips the test, not the module, without the proto package
    req = otlp_json.to_request(body)
    return [sp for rs in req.resource_spans for ss in rs.scope_spans
            for sp in ss.spans]


def _attrs(span):
    out = {}
    for kv in span.attributes:
        which = kv.value.WhichOneof("value")
        out[kv.key] = getattr(kv.value, which)
    return out


def test_through_the_hooks_a_summary_written_after_the_boundary_is_not_lost(
        tmp_path, monkeypatch):
    """The real path: the file grows between two hook runs, the first one
    reads the boundary alone, the second the summary and the call after."""
    home = tmp_path / "home"
    (home / ".claude" / "rius").mkdir(parents=True)
    sign_in(home)
    with open(config.path_rules_path(str(home)), "w") as fh:
        json.dump({"enabled_paths": ["/tmp"]}, fh)
    sent = []
    monkeypatch.setattr(exporter.otlp, "export",
                        lambda ep, key, body, timeout=5.0:
                        sent.append(body) or 200)
    path = tmp_path / "t.jsonl"
    lines = [_boundary_line(trigger="auto", preTokens=900, postTokens=100,
                            durationMs=2000),
             _summary_line("what was said"),
             _assistant_line("a1", "2026-09-30T10:10:31.000Z", "msg_1", 3)]
    path.write_text(json.dumps(lines[0]) + "\n")
    payload = {"session_id": SESSION, "transcript_path": str(path),
               "cwd": "/tmp/proj", "hook_event_name": "PostToolUse"}

    exporter.run("PostToolUse", payload, {}, str(home))
    with open(path, "a") as fh:
        fh.write(json.dumps(lines[1]) + "\n" + json.dumps(lines[2]) + "\n")
    exporter.run("PostToolUse", payload, {}, str(home))

    sent_spans = [sp for body in sent for sp in _otlp_spans(body)]
    compaction = [sp for sp in sent_spans
                  if _attrs(sp).get("gen_ai.operation.name") == "compaction"]
    assert len(compaction) == 2                 # the boundary, then with its summary
    assert compaction[0].span_id == compaction[1].span_id
    assert compaction[0].start_time_unix_nano == compaction[1].start_time_unix_nano
    first, last = _attrs(compaction[0]), _attrs(compaction[1])
    assert "rius.compaction.summary.bytes" not in first
    assert last["rius.compaction.summary.bytes"] == len("what was said")
    assert last["rius.compaction.tokens.dropped"] == 800
    llm = [_attrs(sp) for sp in sent_spans
           if _attrs(sp).get("gen_ai.operation.name") == "chat"]
    assert llm[-1]["gen_ai.conversation.compacted"] is True
