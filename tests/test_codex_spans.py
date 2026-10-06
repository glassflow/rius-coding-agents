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


def _until_first_tool_call(fixtures_dir):
    records, _ = cr.read_from(str(fixtures_dir / FIXTURE), 0)
    return records, next(i for i, r in enumerate(records) if r.kind == cr.TOOL_CALL)


def test_content_turned_off_mid_turn_is_not_sent_when_the_turn_closes(fixtures_dir):
    """The state keeps where the prompt is, so what it can give back is up to
    the capture setting at the time: a folder that stopped sending content
    stops at once, whatever was on when the turn began."""
    records, first_tool = _until_first_tool_call(fixtures_dir)
    state = cs.new_state()
    cs.build(records[:first_tool + 1], state, _ctx(capture=True))
    closing = cs.finalize_session(state, _ctx(capture=False), END_NS)
    for s in closing:
        for key in CONTENT_KEYS:
            assert key not in s.attributes, (s.name, key)
    assert [s for s in closing if s.name == "turn"]


def test_a_secret_file_read_stays_redacted_when_its_call_cannot_be_read_back(
        tmp_path):
    """Whether a call read a secret file is settled when the call is seen,
    not from the line read again at its output: a rollout that has moved
    must not turn into an unredacted `.env`."""
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text("\n".join(json.dumps(line) for line in [
        {"timestamp": "2026-10-05T16:14:00.001Z", "type": "event_msg",
         "payload": {"type": "task_started", "turn_id": "t1"}},
        {"timestamp": "2026-10-05T16:14:00.002Z", "type": "response_item",
         "payload": {"type": "function_call", "call_id": "c1",
                     "name": "exec_command",
                     "arguments": json.dumps({"cmd": "cat .env"})}}]) + "\n")
    records, _ = cr.read_from(str(rollout), 0)
    state, ctx = cs.new_state(), _ctx(capture=True)
    cs.build(records, state, ctx)
    rollout.unlink()
    out = cs.build([cr.Record(cr.TOOL_OUTPUT, 4 * 10**6, {
        "call_id": "c1", "output": "Output:\nDB_PASSWORD=hunter2\n"})],
        state, ctx)
    span = next(s for s in out if s.kind_oi == "TOOL" and not s.pending)
    assert span.attributes["output.value"] == scrub.SECRET_FILE_MARKER
    assert "DB_PASSWORD" not in json.dumps(span.attributes)


def test_a_call_begun_before_an_upgrade_has_its_output_withheld():
    """Its state predates the verdict on whether it read a secret file."""
    state, ctx = cs.new_state(), _ctx(capture=True)
    cs.build([cr.Record(cr.TURN_START, 1, {"turn_id": "t1"}),
              cr.Record(cr.TOOL_CALL, 2, {"call_id": "c1", "name": "exec_command",
                                          "arguments": "{}"})], state, ctx)
    del state["open_tools"]["c1"]["secret_file"]
    out = cs.build([cr.Record(cr.TOOL_OUTPUT, 3, {"call_id": "c1",
                                                  "output": "TOKEN=abc123"})],
                   state, ctx)
    span = next(s for s in out if s.kind_oi == "TOOL" and not s.pending)
    assert span.attributes["output.value"] == cs.OUTPUT_NOT_CHECKED


def test_a_call_begun_with_content_off_has_its_output_withheld_once_it_is_on():
    """Nothing is checked for a secret file while nothing can be sent. The
    folder's choice can change before the output arrives (`content-on-here`
    is itself such a call); with no verdict the output is not sent."""
    state = cs.new_state()
    cs.build([cr.Record(cr.TURN_START, 1, {"turn_id": "t1"}),
              cr.Record(cr.TOOL_CALL, 2, {"call_id": "c1", "name": "exec_command",
                                          "arguments": '{"cmd": "cat .env"}'})],
             state, _ctx(capture=False))
    assert "secret_file" not in state["open_tools"]["c1"]
    out = cs.build([cr.Record(cr.TOOL_OUTPUT, 3, {"call_id": "c1",
                                                  "output": "TOKEN=abc123"})],
                   state, _ctx(capture=True))
    span = next(s for s in out if s.kind_oi == "TOOL" and not s.pending)
    assert span.attributes["output.value"] == cs.OUTPUT_NOT_CHECKED


def test_a_line_that_is_not_where_it_was_is_not_read_as_content(tmp_path):
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text(json.dumps({
        "timestamp": "2026-10-05T16:14:00.001Z", "type": "event_msg",
        "payload": {"type": "user_message", "message": "an unrelated prompt"}})
        + "\n")
    record = cr.read_from(str(rollout), 0)[0][0]
    ctx = _ctx(capture=True)
    here = cs._hold(ctx, record)
    assert cs._recall(ctx, here, "text") == "an unrelated prompt"
    assert cs._recall(ctx, [here[0], here[1], here[2] + 1], "text") == ""
    assert cs._recall(ctx, [str(tmp_path / "gone"), 0, here[2]], "text") == ""


def test_closing_a_trace_with_content_off_reads_no_rollout(fixtures_dir, monkeypatch):
    """The idle sweep closes a trace under whatever the folder says now, and
    a stopped session promises that nothing is read from its transcript."""
    records, first_tool = _until_first_tool_call(fixtures_dir)
    state = cs.new_state()
    cs.build(records[:first_tool + 1], state, _ctx(capture=True))
    assert state["turn"]["text_at"] and state["open_tools"]

    def refuse(path, offset):
        raise AssertionError("the rollout was read")
    monkeypatch.setattr(cr, "read_at", refuse)
    closing = cs.finalize_session(state, _ctx(capture=False), END_NS)
    assert [s for s in closing if s.name == "turn"]


@pytest.mark.parametrize("held", [
    [], ["/x"], ["/x", 1], ["/x", 1, 2, 3], "abc", 5, [None, 0, 1],
    [["a"], 0, 1], ["/x", "0", 1], ["/x", None, 1], ["/x", -5, 1],
    ["/x", 0, "ts"], ["\0", 0, 1], {"a": 1}])
def test_a_place_that_is_not_one_reads_as_nothing(held):
    assert cs._recall(_ctx(capture=True), held, "text") == ""


def test_a_damaged_place_in_the_state_does_not_stop_the_trace(fixtures_dir):
    records, first_tool = _until_first_tool_call(fixtures_dir)
    state = cs.new_state()
    cs.build(records[:first_tool + 1], state, _ctx(capture=True))
    state["turn"]["text_at"] = ["/x", None]
    state["turn"]["reply_at"] = [5]
    state["open_tools"][next(iter(state["open_tools"]))]["input_at"] = "damaged"
    closing = cs.finalize_session(state, _ctx(capture=True), END_NS)
    assert {s.name for s in closing if not s.pending} >= {"turn"}


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
    from tests import otlp_json
    out, state = _build(fixtures_dir)
    body = otlp.encode(dict(cs.resource_attributes(state), **{"service.name": "codex"}), out)
    req = otlp_json.to_request(body)
    decoded = req.resource_spans[0].scope_spans[0].spans
    assert len(decoded) == len(out)
    llm = [s for s in decoded if s.name == "mock-model"][0]
    attrs = {a.key: a.value for a in llm.attributes}
    assert attrs["gen_ai.usage.cache_read.input_tokens"].int_value > 0
    assert decoded[0].trace_id.hex() == spans.trace_id_for(THREAD)


# --- secrets in content mode -------------------------------------------------

_AWS_KEY = "AKIA" + "ABCDEFGHIJKLMNOP"


def _failed_call(tmp_path, name, arguments, output, mcp_error=None):
    """One call that failed, read from a rollout file the way a hook does:
    what the state keeps of the call's content is a place in that file."""
    def line(ms, kind, payload):
        return json.dumps({"timestamp": "2026-10-05T16:14:00.%03dZ" % ms,
                           "type": kind, "payload": payload})
    lines = [line(1, "event_msg", {"type": "task_started", "turn_id": "t1"}),
             line(2, "response_item", {"type": "function_call", "call_id": "c1",
                                       "name": name, "arguments": arguments})]
    if mcp_error is not None:
        lines.append(line(3, "event_msg", {"type": "mcp_tool_call_end",
                                            "call_id": "c1",
                                            "result": {"Err": mcp_error}}))
    lines.append(line(4, "response_item", {"type": "function_call_output",
                                           "call_id": "c1", "output": output}))
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text("\n".join(lines) + "\n")
    records, _ = cr.read_from(str(rollout), 0)
    ctx = spans.Ctx("s1", "/x", "", "", "codex", True, 32768)
    out = cs.build(records, cs.new_state(), ctx)
    return next(s for s in out if s.kind_oi == "TOOL" and not s.pending)


def test_a_failed_commands_error_line_is_scrubbed(tmp_path):
    span = _failed_call(tmp_path, "exec_command", '{"cmd": "deploy"}',
                        "Process exited with code 1\nOutput:\nerror: bad key %s\n"
                        % _AWS_KEY)
    assert span.status_message == "error: bad key [redacted:aws-key]"
    assert span.events[0][2]["exception.message"] == span.status_message
    assert _AWS_KEY not in json.dumps(span.attributes)


def test_an_mcp_error_is_scrubbed(tmp_path):
    span = _failed_call(tmp_path, "mcp__db__query", "{}", "[]",
                        mcp_error="auth failed token=abcdef123456")
    assert span.status_message == "auth failed token=[redacted:token]"


@pytest.mark.parametrize("arguments", [
    '{"cmd": "cat .env"}',
    '{"command": ["bash", "-lc", "cat config/id_rsa"]}',
    '{"path": "/repo/.env.local"}',
])
def test_a_secret_file_read_is_replaced_whole(tmp_path, arguments):
    span = _failed_call(tmp_path, "exec_command", arguments,
                        "Process exited with code 1\nOutput:\nONLY_IN_THE_FILE=1\n")
    assert span.attributes["output.value"] == scrub.SECRET_FILE_MARKER
    assert span.status_message == "exec_command.exit_1"
    assert "ONLY_IN_THE_FILE" not in json.dumps([span.attributes, span.events])


def test_a_deeply_nested_mcp_error_output_is_read_as_text():
    assert cs._mcp_text("[" * 12000 + "]" * 12000) == "[" * 12000 + "]" * 12000
