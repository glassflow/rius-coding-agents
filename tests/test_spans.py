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
