"""Every Claude Code tool span names its call with `gen_ai.tool.call.id`,
the id the model gave the tool_use and the tool_result answers to, so a
span can be joined to anything else that names the same call. Cursor's
spans always did."""
from rius_cc import spans, transcript
from rius_cc import _semconv_generated as semconv

FIXTURE = "tool_error.jsonl"


def _build(fixtures_dir, entries=None, capture=False):
    if entries is None:
        entries, _ = transcript.read_from(str(fixtures_dir / FIXTURE), 0)
    ctx = spans.Ctx(session_id="33333333-3333-3333-3333-333333333333",
                    cwd="/tmp/proj", git_branch="main", cc_version="2.1.278",
                    service_name="claude-code", capture_content=capture,
                    max_attr_bytes=32768)
    st = {"offset": 0, "open_tools": {}, "open_turns": {},
          "root_started": False, "root_start_ns": 0, "last_ns": 0,
          "open_task_spans": []}
    return spans.build(entries, st, ctx), st, ctx


def test_the_pending_and_the_finished_tool_span_name_the_call(fixtures_dir):
    out, _, _ = _build(fixtures_dir)
    tools = [s for s in out if s.kind_oi == "TOOL"]
    assert [s.pending for s in tools] == [True, False]
    assert {s.attributes[semconv.GEN_AI_TOOL_CALL_ID] for s in tools} == {"toolu_1"}


def test_a_tool_closed_at_session_end_names_its_call(fixtures_dir):
    entries, _ = transcript.read_from(str(fixtures_dir / FIXTURE), 0)
    out, st, ctx = _build(fixtures_dir, entries[:2])
    closed = [s for s in spans.finalize_session(st, ctx, entries[1].timestamp_ns + 1)
              if s.kind_oi == "TOOL"]
    assert [s.attributes[semconv.GEN_AI_TOOL_CALL_ID] for s in closed] == ["toolu_1"]
