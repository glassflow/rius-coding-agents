import pytest

from rius_cc import cursor_events, cursor_spans, otlp, spans

from .cursor_fixtures import BASE_NS, STEP_NS, spool

SUB = "5ub00000-0000-4000-8000-000000000002"


def _build(tmp_path, name, capture=True, final_ns=None):
    cid = spool(name, tmp_path, capture_content=capture)
    events = cursor_events.read_conversation(str(tmp_path), cid)
    ctx = cursor_spans.Ctx(cid, capture_content=capture, max_attr_bytes=32768)
    return cid, cursor_spans.build(events, ctx, final_ns=final_ns)


def _by_name(out):
    found = {}
    for s in out:
        found.setdefault(s.name, []).append(s)
    return found


def _one(out, name):
    matches = _by_name(out)[name]
    assert len(matches) == 1, name
    return matches[0]


def _at(step):
    return BASE_NS + step * STEP_NS


def test_ids_are_derived_and_stable():
    """Literal values: a change to the derivation orphans in-flight spans."""
    ids = cursor_spans._Ids("conv-1")
    assert ids.trace == "36524fd8f6747fc2712506d01fee0e18"
    assert ids.root == "0660980be80d90b2"
    assert ids.turn("gen-1") == "1edb8de71af41b72"
    assert ids.llm("gen-1") == "a57b1415adc382d8"
    assert ids.subagent("sub-1") == "6395753f4ec564bf"
    assert ids.root == spans.span_id_for("session:conv-1")


def test_the_tree_matches_the_design(tmp_path):
    cid, out = _build(tmp_path, "docs_session")
    root = _one(out, cursor_spans.DEFAULT_ROOT_NAME)
    turns = _by_name(out)["turn"]
    task = _one(out, "Task")
    explore = _one(out, "explore")
    grep = _one(out, "Grep")

    assert root.parent_span_id is None and root.kind_oi == "AGENT"
    assert {s.trace_id for s in out} == {spans.trace_id_for(cid)}
    assert [t.parent_span_id for t in turns] == [root.span_id] * 2
    for name in ("Shell", "Read", "MCP:list_traces", "Write"):
        assert _one(out, name).parent_span_id == turns[0].span_id
    assert task.parent_span_id == turns[1].span_id
    assert explore.parent_span_id == task.span_id
    assert grep.parent_span_id == explore.span_id
    assert len({s.span_id for s in out}) == len(out)


def test_session_root_closes_at_session_end(tmp_path):
    _, out = _build(tmp_path, "docs_session")
    root = _one(out, cursor_spans.DEFAULT_ROOT_NAME)
    assert not root.pending
    assert (root.start_ns, root.end_ns) == (_at(0), _at(25))
    assert root.attributes["cursor.composer_mode"] == "agent"
    assert root.attributes["cursor.session.reason"] == "completed"
    assert root.attributes["gen_ai.operation.name"] == "invoke_agent"


def test_turn_runs_from_prompt_to_stop_and_carries_the_prompt(tmp_path):
    _, out = _build(tmp_path, "docs_session")
    first = _by_name(out)["turn"][0]
    assert (first.start_ns, first.end_ns) == (_at(1), _at(14))
    assert first.status_code == "OK"
    assert first.attributes["cursor.turn.status"] == "completed"
    assert first.attributes["input.value"].startswith("Find why the invoice")


def test_one_llm_span_per_generation_with_model_and_no_tokens(tmp_path):
    _, out = _build(tmp_path, "docs_session")
    llms = [s for s in out if s.kind_oi == "LLM"]
    assert [s.name for s in llms] == ["claude-4.5-sonnet-thinking", "gpt-5.1-codex"]
    first, second = llms
    assert first.attributes["gen_ai.provider.name"] == "anthropic"
    assert second.attributes["gen_ai.provider.name"] == "openai"
    assert first.attributes["gen_ai.request.model"] == "claude-4.5-sonnet-thinking"
    assert first.attributes["cursor.thinking.duration_ms"] == 1800
    assert first.attributes["output.value"].startswith("Fixed: totals")
    assert (first.start_ns, first.end_ns) == (_at(1), _at(13))
    assert second.attributes["cursor.context.context_tokens"] == 108800
    assert second.attributes["cursor.context.context_window_size"] == 128000
    assert not any(k.startswith("gen_ai.usage.") for s in llms for k in s.attributes)


def test_tool_span_runs_pre_to_post(tmp_path):
    _, out = _build(tmp_path, "docs_session")
    read = _one(out, "Read")
    assert (read.start_ns, read.end_ns) == (_at(6), _at(7))
    assert read.status_code == "OK"
    assert read.attributes["gen_ai.tool.call.id"] == "tool-read-1"
    assert read.attributes["gen_ai.operation.name"] == "execute_tool"
    assert "invoice.py" in read.attributes["input.value"]


def test_a_failing_shell_command_is_an_error_by_exit_code(tmp_path):
    _, out = _build(tmp_path, "docs_session")
    shell = _one(out, "Shell")
    assert shell.status_code == "ERROR"
    assert shell.attributes["error.type"] == "Shell.exit_1"
    assert shell.status_message.startswith("FAILED tests/test_invoice.py")
    assert shell.events[0][1] == "exception"


def test_mcp_failure_is_an_error_by_failure_type(tmp_path):
    _, out = _build(tmp_path, "docs_session")
    mcp = _one(out, "MCP:list_traces")
    assert mcp.status_code == "ERROR"
    assert mcp.attributes["error.type"] == "MCP:list_traces.timeout"
    assert mcp.status_message == "upstream timed out after 30s waiting for list_traces"


def test_subagent_span_names_the_agent(tmp_path):
    _, out = _build(tmp_path, "docs_session")
    explore = _one(out, "explore")
    assert explore.attributes["gen_ai.agent.name"] == "explore"
    assert explore.attributes["gen_ai.request.model"] == "composer-1"
    assert explore.attributes["gen_ai.provider.name"] == "cursor"
    assert explore.attributes["cursor.subagent.tool_call_count"] == 1
    assert explore.attributes["output.value"].startswith("One more float")
    assert (explore.start_ns, explore.end_ns) == (_at(17), _at(20))


def test_unfinished_work_is_pending_without_content(tmp_path):
    _, out = _build(tmp_path, "docs_inflight")
    assert {s.name for s in out} == {cursor_spans.DEFAULT_ROOT_NAME, "turn", "Shell"}
    for s in out:
        assert s.pending and s.status_code == "UNSET"
        assert s.attributes["glassflow.span.pending"] is True
        assert "input.value" not in s.attributes


def test_final_closes_everything_at_that_time(tmp_path):
    _, out = _build(tmp_path, "docs_inflight", final_ns=_at(99))
    assert all(not s.pending and s.end_ns == _at(99) for s in out)
    assert _one(out, "Shell").status_code == "UNSET"


def test_denied_and_interrupted_tools_and_an_errored_session(tmp_path):
    _, out = _build(tmp_path, "docs_error")
    delete = _one(out, "Delete")
    shell = _one(out, "Shell")
    root = _one(out, cursor_spans.DEFAULT_ROOT_NAME)
    assert delete.attributes["error.type"] == "Delete.permission_denied"
    assert shell.status_code == "UNSET"
    assert shell.attributes["cursor.tool.interrupted"] is True
    assert "error.type" not in shell.attributes
    assert _one(out, "turn").status_code == "ERROR"
    assert root.status_code == "ERROR"
    assert root.status_message.startswith("Model provider returned 500")
    assert root.attributes["gen_ai.provider.name"] == "gcp.gemini"
    assert root.attributes["cursor.background"] is True


def test_capture_off_withholds_error_detail(tmp_path):
    _, out = _build(tmp_path, "docs_error", capture=False)
    assert _one(out, "Delete").status_message == spans.TOOL_ERROR_WITHHELD
    root = _one(out, cursor_spans.DEFAULT_ROOT_NAME)
    assert root.status_message == cursor_spans.SESSION_ERROR_WITHHELD


def test_capture_off_keeps_the_shell_failure(tmp_path):
    _, out = _build(tmp_path, "docs_session", capture=False)
    shell = _one(out, "Shell")
    assert shell.attributes["error.type"] == "Shell.exit_1"
    assert shell.status_message == spans.TOOL_ERROR_WITHHELD


def test_tool_start_falls_back_to_its_duration_without_pre_tool_use():
    events = [{"ts": 10 * STEP_NS, "event": "postToolUse", "conversation_id": "c",
               "generation_id": "g", "tool_name": "Read", "tool_use_id": "t",
               "duration": 250}]
    out = cursor_spans.build(events, cursor_spans.Ctx("c", True, 32768))
    read = _one(out, "Read")
    assert read.start_ns == 10 * STEP_NS - 250 * 1000000


def test_a_rebuild_reproduces_ids_and_starts(tmp_path):
    cid, first = _build(tmp_path / "a", "docs_session")
    _, second = _build(tmp_path / "b", "docs_session")
    assert [(s.span_id, s.start_ns) for s in first] == [(s.span_id, s.start_ns) for s in second]


def test_no_events_no_spans():
    assert cursor_spans.build([], cursor_spans.Ctx("c", True, 1)) == []


@pytest.mark.parametrize("model, provider", [
    ("claude-4.5-sonnet-thinking", "anthropic"), ("sonnet-4", "anthropic"),
    ("gpt-5", "openai"), ("gpt-5.1-codex-high", "openai"), ("o3", "openai"),
    ("gemini-3-pro", "gcp.gemini"), ("grok-code-fast-1", "x_ai"),
    ("deepseek-v3.1", "deepseek"), ("composer-1", "cursor"), ("auto", "cursor"),
    ("", "cursor"),
])
def test_provider_for(model, provider):
    assert cursor_spans.provider_for(model) == provider


def test_resource_attributes(tmp_path):
    cid = spool("docs_session", tmp_path)
    events = cursor_events.read_conversation(str(tmp_path), cid)
    assert cursor_spans.resource_attributes(events, "inst-1") == {
        "service.name": "cursor", "service.instance.id": "inst-1",
        "cursor.version": "2026.08.27", "cursor.cwd": "/Users/dev/acme-api",
        "cursor.git_branch": "fix/invoice-rounding"}


def test_spans_encode_and_decode_as_otlp(tmp_path):
    pytest.importorskip("opentelemetry.proto.trace.v1.trace_pb2")
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
        ExportTraceServiceRequest)

    cid = spool("docs_session", tmp_path)
    events = cursor_events.read_conversation(str(tmp_path), cid)
    out = cursor_spans.build(events, cursor_spans.Ctx(cid, True, 32768))
    req = ExportTraceServiceRequest()
    req.ParseFromString(otlp.encode(cursor_spans.resource_attributes(events), out))

    decoded = req.resource_spans[0].scope_spans[0].spans
    assert len(decoded) == len(out)
    shell = next(s for s in decoded if s.name == "Shell")
    assert shell.status.code == 2
