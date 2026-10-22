"""A Codex compaction is a span, and the model call after it says it ran on one.

Codex writes a `compacted` line (a summary, ids, the new context window) and,
right after it, an `event_msg` `context_compacted` marker. After a local
compaction it also re-counts its context itself: a `token_count` whose
`last_token_usage` uses nothing and whose `total_tokens` is its own estimate
of the new context. The `compacted` line becomes a CHAIN span with
gen_ai.operation.name "compaction"; the size it left behind comes from the
first token_count after it, which a later hook may be the one to read.

THE FIXTURE IS SYNTHETIC. No rollout recorded by the installed Codex
(0.162.1) holds a compaction. tests/fixtures/codex/synthetic_compaction.jsonl
is built from the field names and nesting in openai/codex
(codex-rs/history/src/lib.rs, rollout_payload.rs, protocol/src/protocol.rs),
not captured from a real run. It has a local compaction (150000 tokens down
to an estimated 20000) and a remote one (answered by a model response, no
estimate after it), each followed by two model calls.
"""
import json
import shutil

import pytest

import exporter
from rius_cc import agent, codex_rollout as cr
from rius_cc import codex_spans as cs
from rius_cc import config, spans
from rius_cc import _semconv_generated as semconv

from tests import signed_in

FIXTURE = "codex/synthetic_compaction.jsonl"
THREAD = "01a10c00-0000-7000-8000-00000000c0de"
T1 = "01a10c00-0001-7000-8000-000000000001"
T2 = "01a10c00-0002-7000-8000-000000000002"
T0 = 1792490400 * 10**9             # 2026-10-20T10:00:00Z
FLAG = "gen_ai.conversation.compacted"
SUMMARY_1 = "The user asked for the repository layout; src holds the app and tests mirrors it."
SUMMARY_2 = "Summary of the second stretch: the café menu code lives in menu.py."
END_NS = T0 + 3600 * 10**9


def _ctx(capture=True, max_attr_bytes=32768):
    return spans.Ctx(session_id=THREAD, cwd="/tmp/proj", git_branch="",
                     cc_version="", service_name="codex",
                     capture_content=capture, max_attr_bytes=max_attr_bytes)


def _fixture_lines(fixtures_dir):
    return [json.loads(line) for line in
            (fixtures_dir / FIXTURE).read_text(encoding="utf-8").splitlines()]


def _write(tmp_path, lines, name="rollout.jsonl"):
    path = tmp_path / name
    path.write_text("".join(json.dumps(line, ensure_ascii=False) + "\n"
                            for line in lines), encoding="utf-8")
    return path


def _build_path(path, capture=True, cuts=(), round_trip=False, state=None,
                max_attr_bytes=32768, each=None):
    """Every span of a rollout, read in one go or cut after the given line
    numbers, the way successive hooks read a growing file. `round_trip` saves
    the state as JSON between hooks, as the exporter does; `each` is called
    with the state after every read."""
    ctx, st = _ctx(capture, max_attr_bytes), state or cs.new_state()
    lines = path.read_bytes().splitlines(keepends=True)
    stops = [len(b"".join(lines[:n])) for n in cuts] + [None]
    out, offset = [], 0
    for stop in stops:
        records, after = cr.read_from(str(path), offset)
        records = [r for r in records if stop is None or r.offset < stop]
        out += cs.build(records, st, ctx)
        offset = stop if stop is not None else after
        if each:
            each(st)
        if round_trip:
            st = json.loads(json.dumps(st))
    return out, st


def _build(fixtures_dir, **kwargs):
    return _build_path(fixtures_dir / FIXTURE, **kwargs)


def _compactions(out):
    return [s for s in out if s.attributes.get("gen_ai.operation.name") == "compaction"]


def _latest(out):
    """The last copy of each span, by id: what the backend keeps."""
    return {s.span_id: s for s in out}


def _final_compactions(out):
    return sorted(_compactions(list(_latest(out).values())),
                  key=lambda s: s.end_ns)


def _llm(out):
    """The model calls, by the input tokens they were counted with."""
    return {s.attributes["gen_ai.usage.input_tokens"]: s
            for s in out if s.kind_oi == "LLM"}


def _compacted_line(ts="10:00:30.000Z", **payload):
    body = {"message": "sum", "window_id": "win-x"}
    body.update(payload)
    return {"timestamp": "2026-10-20T" + ts, "type": "compacted", "payload": body}


def _count_line(ts, input_tokens, output_tokens=5, total=None, estimate=None,
                window=258400):
    """A token_count line. `total` is the session's running total, which has
    to move for the line to count as a model call."""
    def use(i, o, t):
        return {"input_tokens": i, "cached_input_tokens": 0,
                "output_tokens": o, "reasoning_output_tokens": 0,
                "total_tokens": t}
    last = (use(0, 0, estimate) if estimate is not None
            else use(input_tokens, output_tokens, input_tokens + output_tokens))
    total = total if total is not None else input_tokens + output_tokens
    return {"timestamp": "2026-10-20T" + ts, "type": "event_msg", "payload": {
        "type": "token_count", "info": {
            "total_token_usage": use(total, 0, total),
            "last_token_usage": last, "model_context_window": window}}}


def _turn(lines, turn_id=T1):
    """`lines` inside one open turn, with a session record before it."""
    head = [{"timestamp": "2026-10-20T10:00:00.000Z", "type": "session_meta",
             "payload": {"id": THREAD, "cwd": "/tmp/proj", "cli_version": "0.162.1"}},
            {"timestamp": "2026-10-20T10:00:01.000Z", "type": "event_msg",
             "payload": {"type": "task_started", "turn_id": turn_id}},
            {"timestamp": "2026-10-20T10:00:01.001Z", "type": "turn_context",
             "payload": {"turn_id": turn_id, "model": "mock-model"}}]
    return head + lines


def test_a_local_compaction_is_a_span_with_its_numbers(fixtures_dir):
    out, _ = _build(fixtures_dir, capture=False)
    local, _remote = _final_compactions(out)
    assert local.attributes == {
        "openinference.span.kind": "CHAIN",
        "session.id": THREAD,
        "gen_ai.operation.name": "compaction",
        "rius.compaction.kind": "summarize",
        "rius.compaction.executor": "client",
        "rius.compaction.tokens.before": 150000,      # the last call before it
        "rius.compaction.tokens.after": 20000,        # Codex's own estimate
        "rius.compaction.tokens.dropped": 130000,
        "rius.compaction.context_window": 258400,
        "rius.compaction.id": "win-2",
        "rius.compaction.summary.bytes": len(SUMMARY_1.encode("utf-8")),
    }
    assert local.name == "compaction"
    assert local.kind_oi == "CHAIN"
    assert local.pending is False and local.status_code == "OK"
    assert "rius.compaction.trigger" not in local.attributes


def test_a_remote_compaction_is_done_by_the_provider(fixtures_dir):
    out, _ = _build(fixtures_dir, capture=False)
    _local, remote = _final_compactions(out)
    assert remote.attributes["rius.compaction.executor"] == "provider"
    assert remote.attributes["rius.compaction.kind"] == "summarize"
    assert remote.attributes["rius.compaction.id"] == "win-3"
    # no estimate follows it: the first call's prompt is the size left behind
    assert remote.attributes["rius.compaction.tokens.before"] == 200000
    assert remote.attributes["rius.compaction.tokens.after"] == 18000
    assert remote.attributes["rius.compaction.tokens.dropped"] == 182000
    # bytes, not characters: the e-acute is two
    assert remote.attributes["rius.compaction.summary.bytes"] == len(SUMMARY_2) + 1


def test_the_executor_is_the_provider_only_when_a_response_id_is_set(tmp_path):
    for response_id, executor in (("resp_1", "provider"), (None, "client"),
                                  ("", "client"), (7, "client")):
        path = _write(tmp_path, _turn([_compacted_line(compaction_response_id=response_id)]))
        (span,), = [_compactions(_build_path(path)[0])]
        assert span.attributes["rius.compaction.executor"] == executor, response_id


def test_a_compaction_is_the_moment_of_its_line_under_the_open_turn(fixtures_dir):
    out, _ = _build(fixtures_dir)
    local, remote = _final_compactions(out)
    assert local.start_ns == local.end_ns == T0 + 12 * 10**9
    assert remote.start_ns == remote.end_ns == T0 + 68 * 10**9
    assert local.parent_span_id == spans.span_id_for("turn:" + T1)
    assert remote.parent_span_id == spans.span_id_for("turn:" + T2)
    assert local.span_id == spans.span_id_for("compaction:win-2")
    assert remote.span_id == spans.span_id_for("compaction:win-3")
    assert {s.trace_id for s in out} == {spans.trace_id_for(THREAD)}


def test_only_the_first_call_after_a_compaction_is_flagged(fixtures_dir):
    out, _ = _build(fixtures_dir)
    calls = _llm(out)
    assert len(calls) == 7
    assert calls[100000].attributes.get(FLAG) is None       # before any
    assert calls[150000].attributes.get(FLAG) is None       # the summarizing call
    assert calls[21000].attributes[FLAG] is True            # first after #1
    assert calls[22000].attributes.get(FLAG) is None
    assert calls[200000].attributes.get(FLAG) is None
    assert calls[18000].attributes[FLAG] is True            # first after #2
    assert calls[18500].attributes.get(FLAG) is None
    assert FLAG not in _final_compactions(out)[0].attributes


def test_codex_own_estimate_after_a_compaction_is_no_call_and_takes_no_flag(fixtures_dir):
    """total_tokens=20000, nothing used: no model call is behind it, so no
    LLM span, and the flag waits for the call that really ran."""
    out, _ = _build(fixtures_dir)
    assert 20000 not in _llm(out)
    assert sum(1 for s in out if s.kind_oi == "LLM") == 7
    first_real = _llm(out)[21000]
    assert first_real.attributes[FLAG] is True
    # the sum of what was used is the sum of the real calls
    assert sum(s.attributes["gen_ai.usage.output_tokens"]
               for s in out if s.kind_oi == "LLM") == 40 + 60 + 30 + 20 + 50 + 25 + 15


def test_a_rollout_cut_anywhere_gives_the_same_spans_as_one_pass(fixtures_dir):
    whole, _ = _build(fixtures_dir)
    expected = {sid: (s.attributes, s.start_ns, s.end_ns, s.parent_span_id)
                for sid, s in _latest(whole).items() if not s.pending}
    count = len((fixtures_dir / FIXTURE).read_text().splitlines())
    for cut in range(1, count):
        for trip in (False, True):
            out, _ = _build(fixtures_dir, cuts=[cut], round_trip=trip)
            got = {sid: (s.attributes, s.start_ns, s.end_ns, s.parent_span_id)
                   for sid, s in _latest(out).items() if not s.pending}
            assert got == expected, (cut, trip)
    # a hook per line, the state saved as JSON between them
    out, _ = _build(fixtures_dir, cuts=range(1, count), round_trip=True)
    assert {sid: (s.attributes, s.start_ns, s.end_ns, s.parent_span_id)
            for sid, s in _latest(out).items() if not s.pending} == expected
    assert [s.attributes.get(FLAG) for s in out if s.kind_oi == "LLM"] \
        == [s.attributes.get(FLAG) for s in whole if s.kind_oi == "LLM"]


def test_the_size_after_is_sent_again_under_the_same_id_when_it_arrives(fixtures_dir):
    lines = (fixtures_dir / FIXTURE).read_text().splitlines()
    cut = next(i for i, line in enumerate(lines) if '"compacted"' in line) + 1
    out, st = _build(fixtures_dir, cuts=[cut], round_trip=True)
    sent = _compactions(out)
    first, last = sent[0], _final_compactions(out)[0]
    assert len(sent) == 3        # the line, then its size after, then the remote one's two
    assert "rius.compaction.tokens.after" not in first.attributes
    assert last.span_id == first.span_id
    assert (last.start_ns, last.end_ns) == (first.start_ns, first.end_ns)
    assert last.attributes["rius.compaction.tokens.after"] == 20000
    assert {k: v for k, v in last.attributes.items()
            if not k.startswith("rius.compaction.tokens.after")
            and k != "rius.compaction.tokens.dropped"} == first.attributes


def test_replaying_the_rollout_gives_the_same_span_ids(fixtures_dir):
    first, _ = _build(fixtures_dir)
    again, _ = _build(fixtures_dir)
    assert [(s.span_id, s.start_ns, s.end_ns) for s in _compactions(first)] \
        == [(s.span_id, s.start_ns, s.end_ns) for s in _compactions(again)]
    assert [s.span_id for s in first] == [s.span_id for s in again]
    assert len({s.span_id for s in _compactions(first)}) == 2


def test_the_id_falls_back_to_the_time_when_there_is_no_window_id(tmp_path):
    lines = _turn([_compacted_line(ts="10:00:30.000Z", window_id=None),
                   _compacted_line(ts="10:00:40.000Z", window_id=None)])
    (a, b) = _compactions(_build_path(_write(tmp_path, lines))[0])
    assert "rius.compaction.id" not in a.attributes
    assert a.span_id == spans.span_id_for("compaction:%d" % (T0 + 30 * 10**9))
    assert a.span_id != b.span_id


def test_reading_a_compacted_line_again_flags_no_later_call(fixtures_dir, tmp_path):
    lines = _fixture_lines(fixtures_dir)
    upto = next(i for i, line in enumerate(lines) if line["type"] == "compacted") + 1
    first = _write(tmp_path, lines[:upto], "first.jsonl")
    _, st = _build_path(first)
    # the offset is rewound (or the file replaced): the same lines again
    out, st = _build_path(first, state=st)
    assert _compactions(out) == []
    # The calls from before the compaction are not the first one after it ...
    assert all(s.attributes.get(FLAG) is None for s in out if s.kind_oi == "LLM")
    assert st["open_compaction"] is not None and st["compact_next"] is True
    # ... the one that follows it is, once.
    rest = _write(tmp_path, lines[:upto + 3], "rest.jsonl")
    out, st = _build_path(rest, state=st)
    calls = [s for s in out if s.kind_oi == "LLM"]
    assert [s.attributes.get(FLAG) for s in calls[-1:]] == [True]
    assert [s.attributes.get(FLAG) for s in calls[:-1]] == [None, None]
    (span,) = _compactions(out)
    assert span.attributes["rius.compaction.tokens.after"] == 20000
    out, st = _build_path(rest, state=st)
    assert _compactions(out) == []
    assert all(s.attributes.get(FLAG) is None for s in out if s.kind_oi == "LLM")


def test_a_newer_compaction_takes_the_size_after_not_the_older_one(tmp_path):
    lines = _turn([
        _count_line("10:00:05.000Z", 9000, total=9000),
        _compacted_line(ts="10:00:10.000Z", window_id="old"),
        _compacted_line(ts="10:00:11.000Z", window_id="new"),
        _count_line("10:00:12.000Z", 0, estimate=700),
        _count_line("10:00:20.000Z", 800, total=9800),
    ])
    out, _ = _build_path(_write(tmp_path, lines))
    old, new = _final_compactions(out)
    assert old.attributes["rius.compaction.id"] == "old"
    assert "rius.compaction.tokens.after" not in old.attributes
    assert new.attributes["rius.compaction.id"] == "new"
    assert new.attributes["rius.compaction.tokens.after"] == 700
    # one flag for the two: the older one is not owed a call of its own
    assert [s.attributes.get(FLAG) for s in out if s.kind_oi == "LLM"] == [None, True]


def test_a_count_that_repeats_the_last_call_is_not_the_size_after(tmp_path):
    """Codex re-sends the last usage with a rate-limit update: the running
    total does not move, so it is neither a call nor the new context."""
    lines = _turn([
        _count_line("10:00:05.000Z", 9000, total=9000),
        _compacted_line(ts="10:00:10.000Z"),
        _count_line("10:00:11.000Z", 9000, total=9000),      # a repeat
        _count_line("10:00:20.000Z", 800, total=9800),
    ])
    out, _ = _build_path(_write(tmp_path, lines))
    (span,) = _final_compactions(out)
    assert span.attributes["rius.compaction.tokens.after"] == 800
    assert [s.attributes.get(FLAG) for s in out if s.kind_oi == "LLM"] == [None, True]


def test_the_summary_text_is_content_and_follows_the_capture_gate(fixtures_dir):
    off, st_off = _build(fixtures_dir, capture=False)
    for span in _compactions(off):
        assert "rius.compaction.summary.text" not in span.attributes
        assert "rius.compaction.summary.bytes" in span.attributes
    on, _ = _build(fixtures_dir, capture=True)
    local, remote = _final_compactions(on)
    assert local.attributes["rius.compaction.summary.text"] == SUMMARY_1
    assert remote.attributes["rius.compaction.summary.text"] == SUMMARY_2
    assert "rius.compaction.summary.text" in semconv.CONTENT_ATTRIBUTES
    assert "rius.compaction.summary.bytes" not in semconv.CONTENT_ATTRIBUTES


def test_the_summary_text_never_waits_in_the_state_file(fixtures_dir):
    path = fixtures_dir / FIXTURE
    count = len(path.read_text().splitlines())
    blobs = []
    for capture in (True, False):
        for cut in range(1, count):
            _build_path(path, capture=capture, cuts=[cut],
                        each=lambda st: blobs.append(json.dumps(st, ensure_ascii=False)))
    assert len(blobs) == 2 * 2 * (count - 1)
    for blob in blobs:
        assert "repository layout" not in blob
        assert "menu code lives" not in blob
    # With capture off, nothing of where to read it from is kept either.
    seen = []
    _build_path(path, capture=False, cuts=range(1, count),
                each=lambda st: seen.append(st["open_compaction"]))
    held = [p for p in seen if p]
    assert held and all(p["summary_at"] is None for p in held)
    seen = []
    _build_path(path, capture=True, cuts=range(1, count),
                each=lambda st: seen.append(st["open_compaction"]))
    assert any(p and p["summary_at"] for p in seen)


def test_the_summary_text_is_capped_and_scrubbed_like_other_content(tmp_path):
    secret = "AKIA" + "ABCDEFGHIJKLMNOP"
    text = "keys: %s then a long tail %s" % (secret, "x" * 200)
    path = _write(tmp_path, _turn([_compacted_line(message=text)]))
    (span,) = _compactions(_build_path(path, max_attr_bytes=40)[0])
    shown = span.attributes["rius.compaction.summary.text"]
    assert secret not in shown
    assert "…[truncated" in shown
    assert len(shown.encode("utf-8")) < len(text.encode("utf-8"))
    # The size is the whole summary's, not what was kept.
    assert span.attributes["rius.compaction.summary.bytes"] == len(text.encode("utf-8"))


def test_a_summary_that_cannot_be_read_again_is_sent_without_its_text(fixtures_dir, tmp_path):
    """The text is read from the rollout when the span is built; a rollout
    that is gone by then leaves the size, not a guess."""
    work = tmp_path / "rollout.jsonl"
    shutil.copy(str(fixtures_dir / FIXTURE), str(work))
    lines = work.read_bytes().splitlines(keepends=True)
    cut = next(i for i, line in enumerate(lines) if b'"compacted"' in line) + 1
    ctx, st = _ctx(), cs.new_state()
    records, offset = cr.read_from(str(work), 0)
    before = [r for r in records if r.offset < len(b"".join(lines[:cut]))]
    cs.build(before, st, ctx)
    work.write_bytes(b"")                       # the file is gone under the state
    after = [r for r in records if r.offset >= len(b"".join(lines[:cut]))]
    out = cs.build(after[:1], st, ctx)
    (span,) = [s for s in out if s.attributes.get("gen_ai.operation.name") == "compaction"]
    assert "rius.compaction.summary.text" not in span.attributes
    assert span.attributes["rius.compaction.summary.bytes"] == len(SUMMARY_1.encode("utf-8"))


@pytest.mark.parametrize("payload, absent", [
    ({}, ["summary.bytes", "id"]),
    ({"message": None}, ["summary.bytes"]),
    ({"message": ["not", "text"]}, ["summary.bytes"]),
    ({"message": ""}, ["summary.bytes"]),
    ({"window_id": 4}, ["id"]),                      # an older Codex wrote a number
    ({"window_id": ["w"]}, ["id"]),
    ({"compaction_response_id": {"x": 1}}, []),
    ({"latest_token_usage_record": "garbage"}, ["tokens.before"]),
    ({"latest_token_usage_record": {"usage": [1]}}, ["tokens.before"]),
    ({"latest_token_usage_record": {"usage": {"input_tokens": "7"}}}, ["tokens.before"]),
    ({"latest_token_usage_record": {"usage": {"input_tokens": -7}}}, ["tokens.before"]),
    ({"replacement_history": "all", "window_number": "two"}, []),
])
def test_malformed_fields_never_write_garbage(tmp_path, payload, absent):
    body = {"timestamp": "2026-10-20T10:00:30.000Z", "type": "compacted",
            "payload": payload}
    out, _ = _build_path(_write(tmp_path, _turn([body])))
    (span,) = _compactions(out)
    for name in absent:
        assert "rius.compaction." + name not in span.attributes
    for key, value in span.attributes.items():
        if key.startswith("rius.compaction.tokens."):
            assert isinstance(value, int) and not isinstance(value, bool)
            assert value > 0
    # What is always known is always there.
    assert span.attributes["rius.compaction.kind"] == "summarize"
    assert span.attributes["rius.compaction.executor"] in ("client", "provider")
    assert span.parent_span_id == spans.span_id_for("turn:" + T1)


def test_a_payload_that_is_not_a_dict_or_has_no_time_is_no_record():
    for line in ('{"type": "compacted", "payload": "text"}',
                 '{"type": "compacted", "payload": [1]}',
                 '{"type": "compacted"}',
                 '{"timestamp": "yesterday", "type": "compacted", "payload": {}}'):
        assert cr.parse_line(line) is None


def test_sizes_that_are_not_sizes_are_not_sent(tmp_path):
    lines = _turn([
        _compacted_line(ts="10:00:10.000Z"),
        {"timestamp": "2026-10-20T10:00:11.000Z", "type": "event_msg", "payload": {
            "type": "token_count", "info": {
                "total_token_usage": {"total_tokens": "many"},
                "last_token_usage": {"input_tokens": "x", "output_tokens": None,
                                     "total_tokens": True},
                "model_context_window": "big"}}},
    ])
    out, _ = _build_path(_write(tmp_path, lines))
    (span,) = _final_compactions(out)
    assert not [k for k in span.attributes if k.startswith("rius.compaction.tokens.")]
    assert "rius.compaction.context_window" not in span.attributes


def test_the_size_before_falls_back_to_what_the_line_recorded(tmp_path):
    """A session traced from the middle has seen no count before the line."""
    record = {"usage": {"input_tokens": 4321}}
    lines = _turn([_compacted_line(latest_token_usage_record=record),
                   _count_line("10:00:40.000Z", 0, estimate=321)])
    (span,) = _final_compactions(_build_path(_write(tmp_path, lines))[0])
    assert span.attributes["rius.compaction.tokens.before"] == 4321
    assert span.attributes["rius.compaction.tokens.dropped"] == 4000
    # a count seen before the line wins over it
    lines = _turn([_count_line("10:00:05.000Z", 9000, total=9000),
                   _compacted_line(latest_token_usage_record=record)])
    (span,) = _final_compactions(_build_path(_write(tmp_path, lines))[0])
    assert span.attributes["rius.compaction.tokens.before"] == 9000


def test_no_dropped_count_when_the_context_grew(tmp_path):
    lines = _turn([_count_line("10:00:05.000Z", 100, total=100),
                   _compacted_line(), _count_line("10:00:40.000Z", 0, estimate=250)])
    (span,) = _final_compactions(_build_path(_write(tmp_path, lines))[0])
    assert span.attributes["rius.compaction.tokens.before"] == 100
    assert span.attributes["rius.compaction.tokens.after"] == 250
    assert "rius.compaction.tokens.dropped" not in span.attributes


def test_a_context_left_the_same_size_drops_zero(tmp_path):
    lines = _turn([_count_line("10:00:05.000Z", 250, total=250),
                   _compacted_line(), _count_line("10:00:40.000Z", 0, estimate=250)])
    (span,) = _final_compactions(_build_path(_write(tmp_path, lines))[0])
    assert span.attributes["rius.compaction.tokens.dropped"] == 0


def test_a_compaction_with_nothing_after_it_is_still_a_span(tmp_path):
    path = _write(tmp_path, _turn([_count_line("10:00:05.000Z", 9000, total=9000),
                                   _compacted_line()]))
    out, st = _build_path(path)
    (span,) = _compactions(out)
    assert span.attributes["rius.compaction.tokens.before"] == 9000
    assert "rius.compaction.tokens.after" not in span.attributes
    assert st["open_compaction"] is not None and st["compact_next"] is True
    # closing the session leaves it as it was sent
    done = cs.finalize_session(st, _ctx(), END_NS)
    assert _compactions(done) == []


def test_a_compaction_outside_any_turn_is_dropped_and_flags_nothing(tmp_path):
    """Before this session was traced, or inside a turn a fork copied."""
    lines = _turn([])[:1] + [_compacted_line()] + _turn([_count_line(
        "10:00:40.000Z", 500, total=500)])[1:]
    out, st = _build_path(_write(tmp_path, lines))
    assert _compactions(out) == []
    assert st["compact_next"] is False
    assert [s.attributes.get(FLAG) for s in out if s.kind_oi == "LLM"] == [None]


def test_a_turn_inherited_from_a_fork_brings_no_compaction(tmp_path):
    st = cs.new_state()
    st["forked_at_ms"] = 2 ** 47                 # after the turn's UUIDv7 time
    path = _write(tmp_path, _turn([_count_line("10:00:05.000Z", 9000, total=9000),
                                   _compacted_line()]))
    out, st = _build_path(path, state=st)
    assert _compactions(out) == []
    assert st["compact_next"] is False


def test_a_subagents_compaction_has_an_id_of_its_own(fixtures_dir):
    parent, _ = _build(fixtures_dir)
    sub_state = cs.new_subagent_state("0" * 8 + "-0000-7000-8000-" + "0" * 12,
                                      "default", "p" * 16)
    sub, _ = _build(fixtures_dir, state=sub_state)
    assert {s.span_id for s in _compactions(sub)} \
        .isdisjoint({s.span_id for s in _compactions(parent)})
    assert len({s.span_id for s in _compactions(sub)}) == 2
    assert _llm(sub)[21000].attributes[FLAG] is True


def test_a_state_saved_before_compactions_were_known_still_builds(fixtures_dir):
    old = cs.new_state()
    for key in ("open_compaction", "compact_next", "last_compaction",
                "context_tokens", "context_window"):
        del old[key]
    cs.upgrade_state(old)
    assert old["open_compaction"] is None and old["compact_next"] is False
    out, _ = _build(fixtures_dir, state=old)
    assert len(_final_compactions(out)) == 2


def test_the_context_compacted_marker_is_not_a_record(fixtures_dir):
    records, _ = cr.read_from(str(fixtures_dir / FIXTURE), 0)
    assert [r.kind for r in records].count(cr.COMPACTED) == 2
    marker = ('{"timestamp": "2026-10-20T10:00:12.001Z", "type": "event_msg", '
              '"payload": {"type": "context_compacted"}}')
    assert cr.parse_line(marker) is None
    compacted = [r for r in records if r.kind == cr.COMPACTED]
    assert compacted[0].get("window_id") == "win-2"
    assert compacted[0].get("compaction_response_id") == ""
    assert compacted[0].get("recorded_tokens") == 150000
    assert compacted[1].get("compaction_response_id") == "resp_remote_1"
    usage = [r for r in records if r.kind == cr.USAGE]
    assert [bool(u.get("local_estimate")) for u in usage].count(True) == 1
    estimate = next(u for u in usage if u.get("local_estimate"))
    assert estimate.get("context_tokens") == 20000
    assert estimate.get("input_tokens") == 0


def test_a_real_call_is_not_taken_for_an_estimate():
    for last in ({"input_tokens": 5, "output_tokens": 0, "total_tokens": 5},
                 {"input_tokens": 0, "output_tokens": 3, "total_tokens": 3},
                 {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}):
        line = json.dumps({"timestamp": "2026-10-20T10:00:12.000Z",
                           "type": "event_msg", "payload": {
                               "type": "token_count", "info": {
                                   "total_token_usage": {"total_tokens": 9},
                                   "last_token_usage": last}}})
        assert cr.parse_line(line).get("local_estimate") is False


# --- through the exporter, one hook per growth of the file ---

@pytest.fixture
def codex_home(tmp_path):
    home = tmp_path / ".codex"
    with agent.using(agent.CODEX):
        signed_in.sign_in(str(tmp_path))
        config.write_path_rules(str(tmp_path), {"enabled_paths": ["/tmp"]})
        yield home


@pytest.fixture
def sent(monkeypatch):
    batches, pending = [], {}

    def encode(resource, out):
        pending["batch"] = (resource, list(out))
        return b"x"

    def export(endpoint, key, body, timeout=5.0):
        batches.append(pending.pop("batch"))
        return 200

    monkeypatch.setattr(exporter.otlp, "encode", encode)
    monkeypatch.setattr(exporter.otlp, "export", export)
    return batches


def test_through_the_hooks_the_size_after_is_not_lost(codex_home, tmp_path, sent,
                                                      fixtures_dir):
    """The real path: the rollout grows between two hook runs, the first
    reads up to the compaction, the second the count that follows it."""
    lines = (fixtures_dir / FIXTURE).read_bytes().splitlines(keepends=True)
    cut = next(i for i, line in enumerate(lines) if b'"compacted"' in line) + 2
    rollout = tmp_path / "sessions" / ("rollout-2026-10-20T10-00-00-%s.jsonl" % THREAD)
    rollout.parent.mkdir()

    def hook(event, upto):
        rollout.write_bytes(b"".join(lines[:upto]))
        payload = {"session_id": THREAD, "cwd": "/tmp/proj",
                   "transcript_path": str(rollout), "hook_event_name": event}
        exporter.run(event, payload, {}, str(tmp_path))

    hook("PostToolUse", cut)
    hook("PostToolUse", len(lines))
    hook("Stop", len(lines))

    everything = [s for _, out in sent for s in out]
    sent_compactions = _compactions(everything)
    local = [s for s in sent_compactions if s.attributes["rius.compaction.id"] == "win-2"]
    assert len(local) == 2                      # the line, then with its size after
    assert local[0].span_id == local[1].span_id
    assert "rius.compaction.tokens.after" not in local[0].attributes
    assert local[1].attributes["rius.compaction.tokens.after"] == 20000
    assert local[1].attributes["rius.compaction.summary.text"] == SUMMARY_1
    # sent once: the second hook did not read the line again
    remote = [s for s in sent_compactions if s.attributes["rius.compaction.id"] == "win-3"]
    assert len(remote) == 1 and remote[0].attributes["rius.compaction.tokens.after"] == 18000
    flagged = [s.attributes["gen_ai.usage.input_tokens"]
               for s in everything if s.attributes.get(FLAG)]
    assert flagged == [21000, 18000]
    # the saved state holds ids and sizes, and no summary text
    blob = (codex_home / "rius" / "state" / (THREAD + ".json")).read_text()
    assert "repository layout" not in blob and "menu code lives" not in blob
