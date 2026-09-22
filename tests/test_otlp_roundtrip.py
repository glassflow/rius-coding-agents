"""Decode our hand-rolled encoder's output with the REAL library.

opentelemetry-proto is a test-only dependency. It never ships. Its entire job
is to make it impossible for a field number or wire type to drift silently.
"""
import pytest

from rius_cc import otlp, spans

pytest.importorskip("opentelemetry.proto.trace.v1.trace_pb2")

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (  # noqa: E402
    ExportTraceServiceRequest,
)


def _decode(body):
    req = ExportTraceServiceRequest()
    req.ParseFromString(body)
    return req


def test_roundtrip_full_span():
    s = spans.Span(
        trace_id="0123456789abcdef0123456789abcdef",
        span_id="fedcba9876543210",
        parent_span_id="1122334455667788",
        name="claude-opus-5", kind_oi="LLM",
        start_ns=1758535200000000000, end_ns=1758535202500000000,
        attributes={
            "gen_ai.request.model": "claude-opus-5",
            "gen_ai.usage.input_tokens": 10,
            "gen_ai.usage.output_tokens": 5,
            "glassflow.span.pending": False,
            "gen_ai.response.finish_reasons": ["end_turn"],
        },
        status_code="ERROR", status_message="boom", pending=False)

    req = _decode(otlp.encode({"service.name": "claude-code"}, [s]))

    rs = req.resource_spans[0]
    assert {kv.key: kv.value.string_value for kv in rs.resource.attributes} == {
        "service.name": "claude-code"}
    ss = rs.scope_spans[0]
    assert ss.scope.name == "glassflow"

    got = ss.spans[0]
    assert got.trace_id == bytes.fromhex("0123456789abcdef0123456789abcdef")
    assert got.span_id == bytes.fromhex("fedcba9876543210")
    assert got.parent_span_id == bytes.fromhex("1122334455667788")
    assert got.name == "claude-opus-5"
    assert got.start_time_unix_nano == 1758535200000000000
    assert got.end_time_unix_nano == 1758535202500000000
    assert got.status.code == 2                      # ERROR
    assert got.status.message == "boom"

    attrs = {kv.key: kv.value for kv in got.attributes}
    assert attrs["gen_ai.request.model"].string_value == "claude-opus-5"
    assert attrs["gen_ai.usage.input_tokens"].int_value == 10
    assert attrs["glassflow.span.pending"].bool_value is False
    assert [v.string_value
            for v in attrs["gen_ai.response.finish_reasons"].array_value.values] == ["end_turn"]


def test_roundtrip_root_span_has_empty_parent():
    s = spans.Span(trace_id="a" * 32, span_id="b" * 16, parent_span_id=None,
                   name="claude-code session", kind_oi="AGENT",
                   start_ns=1, end_ns=2, attributes={}, status_code=None,
                   status_message=None, pending=True)
    got = _decode(otlp.encode({}, [s])).resource_spans[0].scope_spans[0].spans[0]
    assert got.parent_span_id == b""


def test_roundtrip_many_spans_preserves_order():
    made = [spans.Span(trace_id="a" * 32, span_id="%016x" % i,
                       parent_span_id=None, name="s%d" % i, kind_oi="CHAIN",
                       start_ns=i + 1, end_ns=i + 2, attributes={},
                       status_code=None, status_message=None, pending=False)
            for i in range(25)]
    got = _decode(otlp.encode({}, made)).resource_spans[0].scope_spans[0].spans
    assert [s.name for s in got] == ["s%d" % i for i in range(25)]
