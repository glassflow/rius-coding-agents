"""Decode our OTLP/JSON output into the REAL opentelemetry-proto messages.

opentelemetry-proto is a test-only dependency. It never ships. Its entire job
is to make it impossible for a field name or JSON shape to drift silently:
json_format.ParseDict rejects unknown fields and wrongly typed values.
"""
import json

import pytest

from rius_cc import otlp, spans

pytest.importorskip("opentelemetry.proto.trace.v1.trace_pb2")

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (  # noqa: E402
    ExportTraceServiceRequest,
)

from tests import otlp_json  # noqa: E402

_decode = otlp_json.to_request


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


def _decoded_attrs(attributes):
    s = spans.Span(trace_id="a" * 32, span_id="b" * 16, parent_span_id=None,
                   name="n", kind_oi="LLM", start_ns=1, end_ns=2,
                   attributes=attributes, status_code=None,
                   status_message=None, pending=False)
    got = _decode(otlp.encode({}, [s])).resource_spans[0].scope_spans[0].spans[0]
    return {kv.key: kv.value for kv in got.attributes}


@pytest.mark.parametrize("value, member, expected", [
    (0, "int_value", 0),
    (0.0, "double_value", 0.0),
    (-0.0, "double_value", 0.0),
    ("", "string_value", ""),
    (False, "bool_value", False),
])
def test_roundtrip_zero_values_keep_their_type(value, member, expected):
    """A first turn reports cache_read_input_tokens: 0. Dropping the oneof
    member makes it arrive as "no value", and a search for = 0 misses it."""
    got = _decoded_attrs({"v": value})["v"]
    assert got.WhichOneof("value") == member
    assert getattr(got, member) == expected


def test_roundtrip_nested_arrays_keep_zeros():
    got = _decoded_attrs({"v": [0, [0.0, "", False], 3]})["v"]
    outer = got.array_value.values
    assert [v.WhichOneof("value") for v in outer] == [
        "int_value", "array_value", "int_value"]
    inner = outer[1].array_value.values
    assert [v.WhichOneof("value") for v in inner] == [
        "double_value", "string_value", "bool_value"]


def test_roundtrip_int_outside_int64_falls_back_to_digits():
    attrs = _decoded_attrs({"big": 2 ** 64, "small": -2 ** 63 - 1,
                            "max": 2 ** 63 - 1, "min": -2 ** 63})
    assert attrs["big"].string_value == str(2 ** 64)
    assert attrs["small"].string_value == str(-2 ** 63 - 1)
    assert attrs["max"].int_value == 2 ** 63 - 1
    assert attrs["min"].int_value == -2 ** 63


def test_roundtrip_lone_surrogate_is_replaced_not_raised():
    attrs = _decoded_attrs({"k\ud800": "a\udfffb"})
    assert attrs["k?"].string_value == "a?b"


def test_roundtrip_dict_is_json_text_and_bytes_are_bytes():
    attrs = _decoded_attrs({"d": {"a": 1, "b": [0, "x"]}, "raw": b"\x00\x01"})
    assert json.loads(attrs["d"].string_value) == {"a": 1, "b": [0, "x"]}
    assert attrs["raw"].bytes_value == b"\x00\x01"


def test_message_equals_the_reference_message_with_zero_values():
    """Build the expected message straight from the protobuf classes and
    compare it whole: every field, zero and empty value included."""
    from opentelemetry.proto.common.v1.common_pb2 import AnyValue, ArrayValue, KeyValue
    from opentelemetry.proto.trace.v1.trace_pb2 import ResourceSpans, Span as PbSpan

    attributes = {"i": 0, "d": 0.0, "s": "", "b": False, "arr": [0, ""], "n": 7}
    ours = _decode(otlp.encode({}, [spans.Span(
        trace_id="a" * 32, span_id="b" * 16, parent_span_id=None, name="n",
        kind_oi="LLM", start_ns=1, end_ns=2, attributes=attributes,
        status_code=None, status_message=None, pending=False)]))

    rs = ResourceSpans()
    rs.resource.SetInParent()
    ss = rs.scope_spans.add()
    ss.scope.name = otlp.SCOPE_NAME
    pb = ss.spans.add()
    pb.trace_id, pb.span_id, pb.name = bytes.fromhex("a" * 32), bytes.fromhex("b" * 16), "n"
    pb.kind = PbSpan.SPAN_KIND_INTERNAL
    pb.start_time_unix_nano, pb.end_time_unix_nano = 1, 2
    pb.attributes.extend([
        KeyValue(key="i", value=AnyValue(int_value=0)),
        KeyValue(key="d", value=AnyValue(double_value=0.0)),
        KeyValue(key="s", value=AnyValue(string_value="")),
        KeyValue(key="b", value=AnyValue(bool_value=False)),
        KeyValue(key="arr", value=AnyValue(array_value=ArrayValue(values=[
            AnyValue(int_value=0), AnyValue(string_value="")]))),
        KeyValue(key="n", value=AnyValue(int_value=7)),
    ])
    expected = ExportTraceServiceRequest(resource_spans=[rs])
    assert ours.SerializeToString(deterministic=True) == \
        expected.SerializeToString(deterministic=True)


def test_roundtrip_events_and_non_finite_doubles():
    s = spans.Span(trace_id="a" * 32, span_id="b" * 16, parent_span_id=None,
                   name="n", kind_oi="LLM", start_ns=1, end_ns=2,
                   attributes={"nan": float("nan"), "inf": float("inf")},
                   status_code=None, status_message=None, pending=False)
    s.events = [(1758535201000000000, "ev", {"k": 0, "t": "x"})]
    got = _decode(otlp.encode({}, [s])).resource_spans[0].scope_spans[0].spans[0]
    attrs = {kv.key: kv.value for kv in got.attributes}
    assert attrs["nan"].double_value != attrs["nan"].double_value
    assert attrs["inf"].double_value == float("inf")
    ev = got.events[0]
    assert (ev.time_unix_nano, ev.name) == (1758535201000000000, "ev")
    assert {kv.key: kv.value.WhichOneof("value") for kv in ev.attributes} == {
        "k": "int_value", "t": "string_value"}


def test_roundtrip_fails_on_base64_ids():
    """The helper must be as strict as the receiver: an id that is not lowercase
    hex of the right length is an error, never silently accepted."""
    import base64
    good = json.loads(otlp.encode({}, [spans.Span(
        trace_id="a" * 32, span_id="b" * 16, parent_span_id=None, name="n",
        kind_oi="LLM", start_ns=1, end_ns=2, attributes={}, status_code=None,
        status_message=None, pending=False)]))
    sp = good["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    sp["traceId"] = base64.b64encode(bytes.fromhex("a" * 32)).decode()
    with pytest.raises(AssertionError):
        _decode(json.dumps(good).encode())
