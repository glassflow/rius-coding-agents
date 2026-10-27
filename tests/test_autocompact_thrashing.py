"""Claude Code's autocompact thrashing stop is an error on the session's trace.

When compaction keeps refilling the context, Claude Code stops the turn and
writes a synthetic assistant row with `apiError: "autocompact_thrashing"`.
No hook carries it, so the transcript row is the only record. It becomes the
LLM span it always was, now ERROR, with error.type and an exception event at
the row's time. tests/fixtures/autocompact_thrashing.jsonl is that row, as
Claude Code 2.1.292 wrote it, after an ordinary call.
"""
import json

from rius_cc import spans, state, transcript
from rius_cc import _semconv_generated as semconv

SESSION = "99999999-9999-9999-9999-999999999999"
THRASHING_NS = 1791449656233000000          # 2026-10-08T08:54:16.233Z
STOP_TEXT = "Autocompact is thrashing: the context refilled to the limit"


def _build(entries, capture=True):
    ctx = spans.Ctx(session_id=SESSION, cwd="/tmp/proj", git_branch="main",
                    cc_version="2.1.304", service_name="claude-code",
                    capture_content=capture, max_attr_bytes=32768)
    return spans.build(entries, state.new_state(), ctx)


def _entries(fixtures_dir, name="autocompact_thrashing.jsonl"):
    return transcript.read_from(str(fixtures_dir / name), 0)[0]


def _llm(out):
    return [s for s in out if s.kind_oi == "LLM"]


def _stops(out):
    return [s for s in out if s.attributes.get(semconv.ERROR_TYPE)]


def test_the_stop_is_an_error_span_with_its_time_and_text(fixtures_dir):
    stops = _stops(_build(_entries(fixtures_dir)))
    assert len(stops) == 1
    stop = stops[0]
    assert stop.kind_oi == "LLM"
    assert stop.status_code == "ERROR"
    assert stop.attributes[semconv.ERROR_TYPE] == "autocompact_thrashing"
    assert stop.end_ns == THRASHING_NS
    [(at, name, attrs)] = stop.events
    assert at == THRASHING_NS
    assert name == semconv.EXCEPTION_EVENT
    assert attrs[semconv.EXCEPTION_TYPE] == "autocompact_thrashing"
    assert attrs[semconv.EXCEPTION_MESSAGE].startswith(STOP_TEXT)
    assert stop.status_message.startswith(STOP_TEXT)


def test_the_stop_text_follows_the_capture_gate(fixtures_dir):
    [stop] = _stops(_build(_entries(fixtures_dir), capture=False))
    assert stop.attributes[semconv.ERROR_TYPE] == "autocompact_thrashing"
    assert stop.status_message == "autocompact_thrashing"
    [(_, _, attrs)] = stop.events
    assert attrs[semconv.EXCEPTION_MESSAGE] == "autocompact_thrashing"
    assert STOP_TEXT not in json.dumps([stop.events, stop.status_message])


def test_the_call_before_the_stop_is_not_marked(fixtures_dir):
    out = _build(_entries(fixtures_dir))
    before = [s for s in _llm(out) if s.name == "claude-opus-5"]
    assert len(before) == 1
    assert before[0].status_code == "OK"
    assert before[0].events == []
    assert semconv.ERROR_TYPE not in before[0].attributes


def test_a_transcript_without_the_row_gets_no_stop(fixtures_dir):
    for name in ("simple.jsonl", "compaction.jsonl", "tool_call.jsonl"):
        out = _build(_entries(fixtures_dir, name))
        assert _stops(out) == []
        assert [s for s in _llm(out) if s.events] == []


def test_other_api_errors_are_left_as_they_were(fixtures_dir):
    lines = (fixtures_dir / "autocompact_thrashing.jsonl").read_text().splitlines()
    row = json.loads(lines[-1])
    row["apiError"] = "rate_limit"
    entries = [transcript.parse_line(json.dumps(r))
               for r in [json.loads(lines[0]), json.loads(lines[1]), row]]
    out = _build(entries)
    assert _stops(out) == []
    assert all(s.status_code == "OK" for s in _llm(out))
