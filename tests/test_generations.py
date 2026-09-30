"""One API response -> ONE generation span, however many transcript lines.

Claude Code writes a single model response as one transcript line per
content block (thinking, text, each tool_use), and every one of those lines
carries the response's `usage`. A span per line counted the same tokens once
per block: a real Opus call showed up six times with the same 3,060 output
tokens. The lines share `message.id`, which is what a generation is keyed on.

The fixture has both shapes seen in Claude Code 2.1.284:
  msg_A  main-transcript shape: 4 lines, identical final usage on each.
  msg_B  streamed (subagent) shape: early lines carry partial usage and no
         stop_reason, and tool_results land between lines of the response.
  msg_C  a one-line response.
"""
import json

from rius_cc import spans, state, transcript

FIXTURE = "multi_line_response.jsonl"


def _ctx(capture=True):
    return spans.Ctx(session_id="77777777-7777-7777-7777-777777777777",
                     cwd="/tmp/proj", git_branch="main", cc_version="2.1.284",
                     service_name="claude-code", capture_content=capture,
                     max_attr_bytes=32768)


def _last_versions(out):
    """The backend keeps the last finished copy of a span id; so do we."""
    latest = {}
    for s in out:
        if s.pending and s.span_id in latest:
            continue
        latest[s.span_id] = s
    return latest


def _whole(fixtures_dir, capture=True):
    path = str(fixtures_dir / FIXTURE)
    entries, _ = transcript.read_from(path, 0)
    st = state.new_state()
    return spans.build(entries, st, _ctx(capture), source_path=path), st


def _one_line_at_a_time(fixtures_dir, capture=True):
    """The real hook cadence: each hook sees whatever was appended since the
    last one, so a response's lines arrive in separate batches."""
    path = str(fixtures_dir / FIXTURE)
    st = state.new_state()
    ctx = _ctx(capture)
    entries, _ = transcript.read_from(path, 0)
    out = []
    for entry in entries:
        out += spans.build([entry], st, ctx, source_path=path)
    return out, st


def _gens(out):
    return [s for s in _last_versions(out).values() if s.kind_oi == "LLM"]


def _by_output(gens):
    return {s.attributes["gen_ai.usage.output_tokens"]: s for s in gens}


def test_one_generation_span_per_api_response(fixtures_dir):
    out, _ = _whole(fixtures_dir)
    llm = [s for s in out if s.kind_oi == "LLM"]
    # Emitted once each within a batch, not once per transcript line.
    assert len(llm) == 3
    assert len({s.span_id for s in llm}) == 3


def test_usage_is_counted_once_per_response(fixtures_dir):
    out, _ = _whole(fixtures_dir)
    gens = _gens(out)
    outputs = sorted(s.attributes["gen_ai.usage.output_tokens"] for s in gens)
    # 300 + 120 + 20; a span per line would have summed 1200 + 122 + 20.
    assert outputs == [20, 120, 300]
    a = _by_output(gens)[300].attributes
    assert a["gen_ai.usage.input_tokens"] == 2 + 1000 + 500
    assert a["gen_ai.usage.cache_read.input_tokens"] == 1000
    assert a["gen_ai.usage.cache_write.input_tokens"] == 500
    assert a["gen_ai.response.finish_reasons"] == ["tool_use"]


def test_streamed_response_takes_its_final_usage(fixtures_dir):
    """msg_B's first two lines say output_tokens=1 and stop_reason=None; only
    its last line has the real count."""
    out, _ = _whole(fixtures_dir)
    b = _by_output(_gens(out))[120]
    assert b.attributes["gen_ai.usage.input_tokens"] == 8 + 1500 + 40
    assert b.attributes["gen_ai.response.finish_reasons"] == ["tool_use"]


def test_tool_spans_parent_to_their_generation(fixtures_dir):
    out, _ = _whole(fixtures_dir)
    gens = _by_output(_gens(out))
    tools = {s.attributes["gen_ai.tool.name"] + ":" + s.span_id: s
             for s in _last_versions(out).values() if s.kind_oi == "TOOL"}
    parents = {}
    for tool_id in ("toolu_A1", "toolu_A2", "toolu_B1", "toolu_B2"):
        sid = spans.span_id_for(tool_id)
        span = [s for k, s in tools.items() if k.endswith(sid)][0]
        assert span.pending is False
        parents[tool_id] = span.parent_span_id
    assert parents["toolu_A1"] == parents["toolu_A2"] == gens[300].span_id
    assert parents["toolu_B1"] == parents["toolu_B2"] == gens[120].span_id


def test_generation_spans_the_whole_response(fixtures_dir):
    out, _ = _whole(fixtures_dir)
    gens = _by_output(_gens(out))
    a, b = gens[300], gens[120]
    # A: from the prompt (10:00:00) to its last line (10:00:02.3).
    assert b.start_ns - a.start_ns == 3_500_000_000
    assert a.end_ns - a.start_ns == 2_300_000_000
    # B: from the tool_result before it (10:00:03.5) to its last line (10:00:07).
    assert b.end_ns - b.start_ns == 3_500_000_000


def test_generation_output_joins_the_text_of_all_its_lines(fixtures_dir):
    out, _ = _whole(fixtures_dir)
    gens = _by_output(_gens(out))
    assert gens[300].attributes["output.value"] == "Reading both files now."
    assert gens[120].attributes["output.value"] == "One more check."


def test_split_across_hooks_converges_on_one_complete_span(fixtures_dir):
    """A response whose lines arrive in several hook events is re-emitted
    under the SAME span id and start time -- the backend keeps the last
    finished copy -- and the last copy is complete: final usage, and the
    text from a line that was read by an earlier hook."""
    out, st = _one_line_at_a_time(fixtures_dir)
    llm = [s for s in out if s.kind_oi == "LLM"]
    assert len({s.span_id for s in llm}) == 3
    for span_id in {s.span_id for s in llm}:
        copies = [s for s in llm if s.span_id == span_id]
        assert len({s.start_ns for s in copies}) == 1, "start must not move"
    whole, _ = _whole(fixtures_dir)
    want = {s.span_id: s for s in _gens(whole)}
    got = {s.span_id: s for s in _gens(out)}
    assert set(got) == set(want)
    for span_id, span in want.items():
        assert got[span_id].attributes == span.attributes
        assert got[span_id].start_ns == span.start_ns
        assert got[span_id].end_ns == span.end_ns
        assert got[span_id].parent_span_id == span.parent_span_id


def test_capture_off_drops_generation_text(fixtures_dir):
    out, st = _one_line_at_a_time(fixtures_dir, capture=False)
    for s in _gens(out):
        assert "output.value" not in s.attributes
    blob = json.dumps(st)
    for secret in ("Reading both files now.", "One more check."):
        assert secret not in blob


def test_lines_without_a_message_id_stay_one_span_each(fixtures_dir):
    """Older transcripts (and hand-written fixtures) have no message.id; each
    line is then its own generation, keyed by its uuid as before."""
    entries, _ = transcript.read_from(str(fixtures_dir / "simple.jsonl"), 0)
    st = state.new_state()
    out = spans.build(entries, st, spans.Ctx(
        session_id=entries[0].session_id, cwd="/tmp/proj", git_branch="main",
        cc_version="2.1.278", service_name="claude-code",
        capture_content=True, max_attr_bytes=32768))
    llm = [s for s in out if s.kind_oi == "LLM"]
    assistant = [e for e in entries if e.kind == "assistant"]
    assert [s.span_id for s in llm] == [spans.span_id_for(e.uuid)
                                        for e in assistant]


def test_input_tokens_include_the_cache_counts(fixtures_dir):
    """Anthropic reports input_tokens EXCLUDING cache reads and writes (2
    fresh tokens next to 50k cached, on a real call). The Rius attribute
    reference, the OTel GenAI conventions and the Rius SDKs all send the
    inclusive total, and the backend's token totals are input + output: sent
    raw, a session's total left out 9.85M cached tokens."""
    out, _ = _whole(fixtures_dir)
    for s in _gens(out):
        a = s.attributes
        assert a["gen_ai.usage.input_tokens"] >= (
            a["gen_ai.usage.cache_read.input_tokens"]
            + a["gen_ai.usage.cache_write.input_tokens"])
    c = _by_output(_gens(out))[20].attributes
    assert c["gen_ai.usage.input_tokens"] == 2 + 1600 + 30
    assert c["gen_ai.usage.cache_read.input_tokens"] == 1600
    assert c["gen_ai.usage.cache_write.input_tokens"] == 30


def test_thinking_tokens_are_reported_as_reasoning_tokens():
    entry = transcript.parse_line(json.dumps({
        "type": "assistant", "uuid": "r1", "timestamp": "2026-09-30T10:00:00Z",
        "sessionId": "s", "message": {
            "id": "msg_R", "model": "claude-opus-5", "content": [],
            "usage": {"input_tokens": 2, "output_tokens": 198,
                      "output_tokens_details": {"thinking_tokens": 45}}}}))
    out = spans.build([entry], state.new_state(), _ctx())
    gen = [s for s in out if s.kind_oi == "LLM"][0]
    assert gen.attributes["gen_ai.usage.reasoning.output_tokens"] == 45
    assert gen.attributes["gen_ai.usage.input_tokens"] == 2
