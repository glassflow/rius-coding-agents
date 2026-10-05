"""Codex records -> the AGENT / CHAIN / LLM / TOOL tree, as spans.py builds it.

The fixture is described in test_codex_rollout.py.
"""
import json

import pytest

from rius_cc import codex_rollout as cr
from rius_cc import codex_spans as cs
from rius_cc import otlp, scrub, spans

FIXTURE = "codex/mock_tools_mcp_resume.jsonl"
THREAD = "01a10b40-afd3-7d51-a63c-9d2d01e8445e"
END_NS = 1791190999 * 10**9
CONTENT_KEYS = ("input.value", "output.value")


def _ctx(capture=True):
    return spans.Ctx(session_id=THREAD, cwd="/tmp/proj", git_branch="",
                     cc_version="", service_name="codex",
                     capture_content=capture, max_attr_bytes=32768)


def _build(fixtures_dir, capture=True, chunks=None):
    """Every span of the fixture, read in one go or split at the given line
    numbers, the way successive hooks read a growing file."""
    path = str(fixtures_dir / FIXTURE)
    ctx, state = _ctx(capture), cs.new_state()
    lines = (fixtures_dir / FIXTURE).read_bytes().splitlines(keepends=True)
    offsets = [len(b"".join(lines[:n])) for n in (chunks or [])] + [None]
    out, offset = [], 0
    for stop in offsets:
        records, offset_after = cr.read_from(path, offset)
        records = [r for r in records if stop is None or r.offset < stop]
        out += cs.build(records, state, ctx)
        offset = stop if stop is not None else offset_after
    return out + cs.finalize_session(state, ctx, END_NS), state


def _finished(out):
    return {s.span_id: s for s in out if not s.pending}


def _of_kind(out, kind):
    return [s for s in _finished(out).values() if s.kind_oi == kind]


def test_the_tree_is_session_turns_calls_tools(fixtures_dir):
    out, _ = _build(fixtures_dir)
    root = spans.span_id_for("session:" + THREAD)
    turns = _of_kind(out, "CHAIN")
    llms = _of_kind(out, "LLM")
    tools = _of_kind(out, "TOOL")

    assert [s.parent_span_id for s in _of_kind(out, "AGENT")] == [None]
    assert len(turns) == 3 and {t.parent_span_id for t in turns} == {root}
    assert [len([c for c in llms if c.parent_span_id == t.span_id]) for t in turns] \
        == [4, 1, 1]
    assert {t.parent_span_id for t in tools} <= {c.span_id for c in llms}
    assert len(tools) == 3
    assert {s.trace_id for s in out} == {spans.trace_id_for(THREAD)}


def test_a_tool_belongs_to_the_model_call_that_asked_for_it(fixtures_dir):
    out, _ = _build(fixtures_dir)
    llms = sorted(_of_kind(out, "LLM"), key=lambda s: s.start_ns)
    tools = sorted(_of_kind(out, "TOOL"), key=lambda s: s.start_ns)
    assert [t.parent_span_id for t in tools] == [c.span_id for c in llms[:3]]
    assert all(c.start_ns <= c.end_ns for c in llms)


def test_every_pending_span_is_finished(fixtures_dir):
    out, _ = _build(fixtures_dir)
    pending = {s.span_id for s in out if s.pending}
    assert pending <= set(_finished(out))


def test_tokens_are_sent_the_way_openai_counts_them(fixtures_dir):
    out, _ = _build(fixtures_dir)
    first = min(_of_kind(out, "LLM"), key=lambda s: s.start_ns)
    a = first.attributes
    # input_tokens already includes the cached tokens: sent as given, with
    # the cache read as its subset. Reasoning is inside output_tokens.
    assert a["gen_ai.usage.input_tokens"] == 6000
    assert a["gen_ai.usage.cache_read.input_tokens"] == 2500
    assert a["gen_ai.usage.output_tokens"] == 15
    assert a["gen_ai.usage.reasoning.output_tokens"] == 5
    assert "gen_ai.usage.cache_write.input_tokens" not in a
    assert a["gen_ai.provider.name"] == "openai"
    assert a["gen_ai.request.model"] == "mock-model"
    assert first.name == "mock-model"


def test_token_totals_match_codex_own_total(fixtures_dir):
    out, _ = _build(fixtures_dir)
    llms = _of_kind(out, "LLM")
    assert sum(s.attributes["gen_ai.usage.input_tokens"] for s in llms) == 51000
    assert sum(s.attributes["gen_ai.usage.cache_read.input_tokens"] for s in llms) == 22500
    assert sum(s.attributes["gen_ai.usage.output_tokens"] for s in llms) == 105


def test_a_repeated_token_count_is_not_another_model_call(tmp_path, fixtures_dir):
    # Codex re-sends its last usage with a rate-limit update; the running
    # total does not move, so no model call is behind it.
    lines = (fixtures_dir / FIXTURE).read_bytes().splitlines(keepends=True)
    doubled = tmp_path / "rollout-doubled.jsonl"
    doubled.write_bytes(b"".join(
        line * 2 if b'"token_count"' in line else line for line in lines))
    ctx, state = _ctx(), cs.new_state()
    records, _ = cr.read_from(str(doubled), 0)
    out = cs.build(records, state, ctx)
    llms = _of_kind(out, "LLM")
    assert len(llms) == 6
    assert sum(s.attributes["gen_ai.usage.input_tokens"] for s in llms) == 51000


def test_a_failed_command_is_typed_by_its_exit_code(fixtures_dir):
    out, _ = _build(fixtures_dir)
    failed = [t for t in _of_kind(out, "TOOL") if t.status_code == "ERROR"]
    command = [t for t in failed if t.name == "exec_command"][0]
    assert command.attributes["error.type"] == "exec_command.exit_1"
    assert command.status_message == "ls: /nonexistent-rius-t3: No such file or directory"
    (_, name, attrs), = command.events
    assert name == "exception"
    assert attrs["exception.type"] == "exec_command.exit_1"


def test_an_mcp_error_is_a_tool_error(fixtures_dir):
    out, _ = _build(fixtures_dir)
    mcp = [t for t in _of_kind(out, "TOOL") if t.name == "mcp__t3echo__echo"][0]
    assert mcp.status_code == "ERROR"
    assert mcp.attributes["error.type"] == "mcp__t3echo__echo.tool_error"
    assert mcp.status_message == "echo failed: no such thing"


def test_a_passing_command_is_ok(fixtures_dir):
    out, _ = _build(fixtures_dir)
    ok = [t for t in _of_kind(out, "TOOL") if t.status_code == "OK"]
    assert len(ok) == 1 and "error.type" not in ok[0].attributes


def test_content_is_sent_with_capture_on(fixtures_dir):
    out, _ = _build(fixtures_dir)
    first_turn = min(_of_kind(out, "CHAIN"), key=lambda s: s.start_ns)
    assert first_turn.attributes["input.value"] == "tools please"
    assert first_turn.attributes["output.value"] == "reply 8"
    assert first_turn.attributes["codex.turn.duration_ms"] > 0


def test_capture_off_sends_and_keeps_no_content(fixtures_dir):
    out, state = _build(fixtures_dir, capture=False, chunks=[12])
    for s in out:
        for key in CONTENT_KEYS:
            assert key not in s.attributes, (s.name, key)
    failed = [t for t in out if t.status_code == "ERROR"]
    assert failed and {t.status_message for t in failed} == {spans.TOOL_ERROR_WITHHELD}
    assert all(e[2]["exception.message"] == spans.TOOL_ERROR_WITHHELD
               for t in failed for e in t.events)


def test_capture_off_keeps_no_content_in_state_mid_turn(fixtures_dir):
    path = str(fixtures_dir / FIXTURE)
    records, _ = cr.read_from(path, 0)
    first_tool = next(i for i, r in enumerate(records) if r.kind == cr.TOOL_CALL)
    state = cs.new_state()
    cs.build(records[:first_tool + 1], state, _ctx(capture=False))
    persisted = json.dumps(state)
    assert state["turn"] is not None and state["open_tools"]
    assert "tools please" not in persisted
    assert "nonexistent-rius-t3" not in persisted


@pytest.mark.parametrize("chunks", [[5], [12, 13], [9, 18, 27, 36], list(range(1, 46))])
def test_resuming_from_an_offset_builds_the_same_spans(fixtures_dir, chunks):
    whole, _ = _build(fixtures_dir)
    split, _ = _build(fixtures_dir, chunks=chunks)

    def shape(out):
        return {k: (s.parent_span_id, s.name, s.start_ns, s.end_ns,
                    s.status_code, s.attributes)
                for k, s in _finished(out).items()}
    assert shape(split) == shape(whole)


def test_an_aborted_turn_is_closed_and_says_so():
    ctx, state = _ctx(), cs.new_state()

    def rec(kind, ns, **fields):
        return cr.Record(kind, ns, fields)
    out = cs.build([
        rec(cr.TURN_START, 1, turn_id="t1"),
        rec(cr.TOOL_CALL, 2, call_id="c1", name="exec_command", arguments="{}"),
        rec(cr.TURN_END, 3, turn_id="t1", last_agent_message="", duration_ms=0,
            ttft_ms=0, aborted=True, reason="interrupted"),
    ], state, ctx)
    turn = [s for s in out if s.kind_oi == "CHAIN" and not s.pending][0]
    tool = [s for s in out if s.kind_oi == "TOOL" and not s.pending][0]
    assert turn.attributes["codex.turn.aborted"] == "interrupted"
    assert tool.end_ns == 3 and tool.status_code == "UNSET"
    assert state["turn"] is None and state["open_tools"] == {}


def test_records_before_any_turn_are_dropped():
    state = cs.new_state()
    out = cs.build([cr.Record(cr.USAGE, 5, {"input_tokens": 1})], state, _ctx())
    assert [s.kind_oi for s in out] == ["AGENT"]


def test_an_empty_session_has_no_root_to_close():
    assert cs.finalize_session(cs.new_state(), _ctx(), END_NS) == []


def test_resource_attributes_come_from_the_session(fixtures_dir):
    _, state = _build(fixtures_dir)
    attrs = cs.resource_attributes(state)
    assert attrs["codex.version"] == "0.144.1"
    assert attrs["codex.cwd"] == "/private/tmp/claude-501/codexr-t3/work"
    assert attrs["codex.originator"] == "codex_exec"


def test_spans_decode_as_otlp(fixtures_dir):
    pytest.importorskip("opentelemetry.proto.trace.v1.trace_pb2")
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
        ExportTraceServiceRequest,
    )
    out, state = _build(fixtures_dir)
    body = otlp.encode(dict(cs.resource_attributes(state), **{"service.name": "codex"}), out)
    req = ExportTraceServiceRequest()
    req.ParseFromString(body)
    decoded = req.resource_spans[0].scope_spans[0].spans
    assert len(decoded) == len(out)
    llm = [s for s in decoded if s.name == "mock-model"][0]
    attrs = {a.key: a.value for a in llm.attributes}
    assert attrs["gen_ai.usage.cache_read.input_tokens"].int_value > 0
    assert decoded[0].trace_id.hex() == spans.trace_id_for(THREAD)


# --- secrets in content mode -------------------------------------------------

_AWS_KEY = "AKIA" + "ABCDEFGHIJKLMNOP"


def _failed_call(name, arguments, output, mcp_error=None):
    ctx = spans.Ctx("s1", "/x", "", "", "codex", True, 32768)
    st = cs.new_state()
    recs = [cr.Record(cr.TURN_START, 1, {"turn_id": "t1"}),
            cr.Record(cr.TOOL_CALL, 2, {"call_id": "c1", "name": name,
                                         "arguments": arguments})]
    if mcp_error is not None:
        recs.append(cr.Record(cr.MCP_RESULT, 3, {"call_id": "c1",
                                                  "is_error": True,
                                                  "error": mcp_error}))
    recs.append(cr.Record(cr.TOOL_OUTPUT, 4, {"call_id": "c1", "output": output}))
    out = cs.build(recs, st, ctx)
    return next(s for s in out if s.kind_oi == "TOOL" and not s.pending)


def test_a_failed_commands_error_line_is_scrubbed():
    span = _failed_call("exec_command", '{"cmd": "deploy"}',
                        "Process exited with code 1\nOutput:\nerror: bad key %s\n"
                        % _AWS_KEY)
    assert span.status_message == "error: bad key [redacted:aws-key]"
    assert span.events[0][2]["exception.message"] == span.status_message
    assert _AWS_KEY not in json.dumps(span.attributes)


def test_an_mcp_error_is_scrubbed():
    span = _failed_call("mcp__db__query", "{}", "[]",
                        mcp_error="auth failed token=abcdef123456")
    assert span.status_message == "auth failed token=[redacted:token]"


@pytest.mark.parametrize("arguments", [
    '{"cmd": "cat .env"}',
    '{"command": ["bash", "-lc", "cat config/id_rsa"]}',
    '{"path": "/repo/.env.local"}',
])
def test_a_secret_file_read_is_replaced_whole(arguments):
    span = _failed_call("exec_command", arguments,
                        "Process exited with code 1\nOutput:\nONLY_IN_THE_FILE=1\n")
    assert span.attributes["output.value"] == scrub.SECRET_FILE_MARKER
    assert span.status_message == "exec_command.exit_1"
    assert "ONLY_IN_THE_FILE" not in json.dumps([span.attributes, span.events])
