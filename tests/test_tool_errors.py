"""A failed tool call says what KIND of failure it was, in a few words.

The backend groups errors by the `exception` span event's `exception.type`
and `exception.message`, and falls back to the span's status message when
there is no event (argus-core packages/clickhouse-db/spans/errorgroups.go).
The plugin sent no event and put the tool's whole output in the status
message, so with capture on an 80-line reference file became an error's
"type", and every distinct output was its own group.

Now each failed tool span carries an `exception` event: the type is the tool
and how it failed (`Bash.exit_1`, `Write.tool_error`), and the message is
the one line that says why. The whole output stays in `output.value`,
behind the capture gate like before.
"""
import pytest

from rius_cc import otlp, spans, transcript

FIXTURE = "tool_errors_bash.jsonl"


def _tools(fixtures_dir, capture=True):
    entries, _ = transcript.read_from(str(fixtures_dir / FIXTURE), 0)
    ctx = spans.Ctx(session_id="99999999-9999-9999-9999-999999999999",
                    cwd="/tmp/proj", git_branch="main", cc_version="2.1.284",
                    service_name="claude-code", capture_content=capture,
                    max_attr_bytes=32768)
    st = {"offset": 0, "open_tools": {}, "open_turns": {},
          "root_started": False, "root_start_ns": 0, "last_ns": 0,
          "open_task_spans": []}
    out = spans.build(entries, st, ctx)
    return {s.span_id: s for s in out if s.kind_oi == "TOOL" and not s.pending}


def _tool(fixtures_dir, tool_id, capture=True):
    return _tools(fixtures_dir, capture)[spans.span_id_for(tool_id)]


def _exception(span):
    events = [e for e in span.events if e[1] == "exception"]
    assert len(events) == 1
    return events[0]


def test_a_failed_command_is_typed_by_its_exit_code(fixtures_dir):
    span = _tool(fixtures_dir, "toolu_py")
    _, _, attrs = _exception(span)
    assert attrs["exception.type"] == "Bash.exit_1"
    assert span.attributes["error.type"] == "Bash.exit_1"
    assert span.status_code == "ERROR"


def test_a_failed_command_says_why_in_one_line(fixtures_dir):
    """The cause of a failed command is where stderr ends: the traceback's
    last line, not its first."""
    span = _tool(fixtures_dir, "toolu_py")
    _, _, attrs = _exception(span)
    assert attrs["exception.message"] == \
        "ValueError: Input must be provided via --input or stdin"
    assert span.status_message == attrs["exception.message"]


def test_the_whole_output_is_never_the_type_or_the_message(fixtures_dir):
    span = _tool(fixtures_dir, "toolu_grep")
    _, _, attrs = _exception(span)
    assert attrs["exception.type"] == "Bash.exit_1"
    assert "\n" not in attrs["exception.message"]
    assert len(attrs["exception.message"].encode()) <= spans.ERROR_MESSAGE_MAX_BYTES + 32
    assert len(span.status_message.encode()) <= spans.ERROR_MESSAGE_MAX_BYTES + 32
    # ...while the output itself is still there, as output.value.
    assert span.attributes["output.value"].count("\n") > 50


def test_a_tool_error_is_typed_by_its_tool(fixtures_dir):
    span = _tool(fixtures_dir, "toolu_write")
    _, _, attrs = _exception(span)
    assert attrs["exception.type"] == "Write.tool_error"
    assert attrs["exception.message"] == \
        "File has not been read yet. Read it first before writing to it."


def test_the_event_is_at_the_moment_the_tool_failed(fixtures_dir):
    span = _tool(fixtures_dir, "toolu_py")
    time_ns, _, _ = _exception(span)
    assert time_ns == span.end_ns


def test_capture_off_keeps_the_type_and_withholds_the_message(fixtures_dir):
    span = _tool(fixtures_dir, "toolu_py", capture=False)
    _, _, attrs = _exception(span)
    assert attrs["exception.type"] == "Bash.exit_1"
    assert attrs["exception.message"] == spans.TOOL_ERROR_WITHHELD
    assert span.status_message == spans.TOOL_ERROR_WITHHELD
    blob = repr(span.events) + repr(span.attributes) + span.status_message
    for secret in ("ValueError", "Traceback", "bench.py"):
        assert secret not in blob


def test_a_successful_tool_has_no_event(fixtures_dir):
    span = _tool(fixtures_dir, "toolu_ok")
    assert span.events == []
    assert "error.type" not in span.attributes


def test_the_event_survives_the_wire(fixtures_dir):
    pytest.importorskip("opentelemetry.proto.trace.v1.trace_pb2")
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
        ExportTraceServiceRequest)
    span = _tool(fixtures_dir, "toolu_py")
    req = ExportTraceServiceRequest()
    req.ParseFromString(otlp.encode({"service.name": "claude-code"}, [span]))
    got = req.resource_spans[0].scope_spans[0].spans[0]
    assert len(got.events) == 1
    event = got.events[0]
    assert event.name == "exception"
    assert event.time_unix_nano == span.end_ns
    attrs = {kv.key: kv.value.string_value for kv in event.attributes}
    assert attrs == {
        "exception.type": "Bash.exit_1",
        "exception.message": "ValueError: Input must be provided via --input or stdin",
    }
