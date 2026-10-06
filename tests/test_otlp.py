import json
from unittest import mock

from rius_cc import net, otlp, spans


def _span(**kw):
    s = spans.Span(
        trace_id="a" * 32, span_id="b" * 16, parent_span_id=None,
        name="n", kind_oi="LLM", start_ns=1_000_000_000, end_ns=2_000_000_000,
        attributes={}, status_code=None, status_message=None, pending=False)
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def _doc(resource=None, span_list=None):
    return json.loads(otlp.encode(resource or {}, span_list or [_span()]))


def _one(**kw):
    return _doc(span_list=[_span(**kw)])["resourceSpans"][0]["scopeSpans"][0]["spans"][0]


def _attrs(**kw):
    return {a["key"]: a["value"] for a in _one(**kw).get("attributes", [])}


def test_the_body_is_one_json_document():
    body = otlp.encode({"service.name": "x"}, [_span()])
    assert isinstance(body, bytes)
    assert list(json.loads(body)) == ["resourceSpans"]


def test_the_content_type_is_json():
    assert otlp.CONTENT_TYPE == "application/json"


def test_ids_are_lowercase_hex_text_not_base64():
    span = _one(trace_id="0123456789abcdef0123456789abcdef",
                span_id="fedcba9876543210", parent_span_id="1122334455667788")
    assert span["traceId"] == "0123456789abcdef0123456789abcdef"
    assert span["spanId"] == "fedcba9876543210"
    assert span["parentSpanId"] == "1122334455667788"


def test_timestamps_are_decimal_strings_not_numbers():
    """int64 is a string in OTLP/JSON: a nanosecond clock exceeds 2**53, so a
    JSON number would lose digits in any double-based parser."""
    span = _one(start_ns=1758535200000000001, end_ns=1758535202500000003)
    assert span["startTimeUnixNano"] == "1758535200000000001"
    assert span["endTimeUnixNano"] == "1758535202500000003"


def test_span_kind_is_the_internal_enum_integer():
    assert _one()["kind"] == 1


def test_scope_name_is_glassflow():
    assert otlp.SCOPE_NAME == "glassflow"
    scope = _doc()["resourceSpans"][0]["scopeSpans"][0]["scope"]
    assert scope == {"name": "glassflow"}


def test_root_span_omits_parent_field():
    assert "parentSpanId" not in _one(parent_span_id=None)
    assert _one(parent_span_id="c" * 16)["parentSpanId"] == "c" * 16


def test_attribute_types_encode_distinctly():
    """Each value must use its OWN AnyValue member, not stringValue."""
    got = _attrs(attributes={"s": "txt", "i": 7, "b": True, "d": 1.5,
                             "arr": ["x", "y"]})
    assert got["s"] == {"stringValue": "txt"}
    assert got["b"] == {"boolValue": True}        # not intValue 1
    assert got["i"] == {"intValue": "7"}          # int64 is a string
    assert got["d"] == {"doubleValue": 1.5}
    assert got["arr"] == {"arrayValue": {"values": [
        {"stringValue": "x"}, {"stringValue": "y"}]}}


def test_bytes_are_base64_in_bytes_value():
    assert _attrs(attributes={"raw": b"\x00\x01"})["raw"] == {"bytesValue": "AAE="}


def test_dicts_are_json_text():
    value = _attrs(attributes={"d": {"a": 1, "b": [0, "x"]}})["d"]
    assert json.loads(value["stringValue"]) == {"a": 1, "b": [0, "x"]}


def test_a_none_attribute_is_an_empty_any_value_not_the_word_none():
    body = otlp.encode({}, [_span(attributes={"k": None})])
    assert b"None" not in body and b"null" not in body
    assert _attrs(attributes={"k": None})["k"] == {}


def test_non_finite_doubles_are_the_spec_strings_not_bare_nan():
    body = otlp.encode({}, [_span(attributes={
        "n": float("nan"), "p": float("inf"), "m": float("-inf")})])
    json.loads(body, parse_constant=lambda c: (_ for _ in ()).throw(
        AssertionError("bare %s is not JSON" % c)))
    got = _attrs(attributes={"n": float("nan"), "p": float("inf"),
                             "m": float("-inf")})
    assert got["n"] == {"doubleValue": "NaN"}
    assert got["p"] == {"doubleValue": "Infinity"}
    assert got["m"] == {"doubleValue": "-Infinity"}


def test_ints_outside_int64_fall_back_to_digits():
    got = _attrs(attributes={"big": 2 ** 64, "max": 2 ** 63 - 1, "min": -2 ** 63,
                             "below": -2 ** 63 - 1})
    assert got["big"] == {"stringValue": str(2 ** 64)}
    assert got["below"] == {"stringValue": str(-2 ** 63 - 1)}
    assert got["max"] == {"intValue": str(2 ** 63 - 1)}
    assert got["min"] == {"intValue": str(-2 ** 63)}


def test_lone_surrogates_are_replaced_so_the_body_is_valid_utf8():
    body = otlp.encode({}, [_span(name="a\ud800b",
                                  attributes={"k\ud800": "x\udfffy"})])
    span = json.loads(body.decode("utf-8"))["resourceSpans"][0][
        "scopeSpans"][0]["spans"][0]
    assert span["name"] == "a?b"
    assert span["attributes"] == [{"key": "k?", "value": {"stringValue": "x?y"}}]


def test_non_ascii_text_survives_as_utf8():
    assert _attrs(attributes={"k": "h\u00e9llo \u2603 \U0001f600"})["k"] == {
        "stringValue": "h\u00e9llo \u2603 \U0001f600"}


def test_status_is_omitted_unless_set():
    assert "status" not in _one()
    assert _one(status_code="ERROR", status_message="boom")["status"] == {
        "code": 2, "message": "boom"}
    assert _one(status_code="OK")["status"] == {"code": 1}
    assert _one(status_code=None, status_message="only text")["status"] == {
        "message": "only text"}


def test_events_carry_string_time_name_and_attributes():
    s = _span()
    s.events = [(1758535201000000000, "ev", {"k": 0}), (5, "bare", {})]
    got = _one_from(s)["events"]
    assert got == [
        {"timeUnixNano": "1758535201000000000", "name": "ev",
         "attributes": [{"key": "k", "value": {"intValue": "0"}}]},
        {"timeUnixNano": "5", "name": "bare"}]


def _one_from(span):
    return _doc(span_list=[span])["resourceSpans"][0]["scopeSpans"][0]["spans"][0]


def test_resource_attributes_are_encoded_like_span_attributes():
    resource = _doc({"service.name": "claude-code", "n": 0})["resourceSpans"][0]["resource"]
    assert resource["attributes"] == [
        {"key": "service.name", "value": {"stringValue": "claude-code"}},
        {"key": "n", "value": {"intValue": "0"}}]


def test_span_order_is_preserved():
    made = [_span(span_id="%016x" % i, name="s%d" % i) for i in range(25)]
    got = _doc(span_list=made)["resourceSpans"][0]["scopeSpans"][0]["spans"]
    assert [s["name"] for s in got] == ["s%d" % i for i in range(25)]


def test_empty_span_list_is_empty_body():
    assert otlp.encode({}, []) == b""


def _fake_urlopen(status=200):
    resp = mock.MagicMock()
    resp.getcode.return_value = status
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    return resp


def test_export_appends_v1_traces_to_bare_endpoint():
    with mock.patch.object(net._OPENER, "open", return_value=_fake_urlopen()) as m:
        otlp.export("https://ingest.example.com", "key", b"body")
        req = m.call_args[0][0]
        assert req.full_url == "https://ingest.example.com/v1/traces"


def test_export_does_not_double_slash_trailing_slash_endpoint():
    with mock.patch.object(net._OPENER, "open", return_value=_fake_urlopen()) as m:
        otlp.export("https://ingest.example.com/", "key", b"body")
        req = m.call_args[0][0]
        assert req.full_url == "https://ingest.example.com/v1/traces"


def test_the_single_retry_waits_first(monkeypatch):
    """Spec section 9: one retry with a SHORT BACKOFF. Retrying instantly
    against a receiver that is restarting just burns both attempts inside
    the same outage."""
    slept = []
    with mock.patch.object(net._OPENER, "open", return_value=_fake_urlopen(503)) as m:
        status = otlp.export("https://ingest.example.com", "key", b"body",
                             sleep=slept.append)
    assert status == 503
    assert m.call_count == 2            # exactly one retry
    assert slept == [otlp.RETRY_DELAY_S]


def test_a_4xx_is_not_retried_at_all(monkeypatch):
    slept = []
    with mock.patch.object(net._OPENER, "open", return_value=_fake_urlopen(401)) as m:
        otlp.export("https://ingest.example.com", "key", b"body", sleep=slept.append)
    assert m.call_count == 1
    assert slept == []


def test_a_success_does_not_sleep():
    slept = []
    with mock.patch.object(net._OPENER, "open", return_value=_fake_urlopen(200)):
        otlp.export("https://ingest.example.com", "key", b"body", sleep=slept.append)
    assert slept == []


def test_zero_and_empty_attributes_still_carry_their_member():
    """RIUS-1225: "0 output tokens" arrived empty because zero was skipped.
    Every zero and empty value must be written, not dropped or nulled."""
    got = _attrs(attributes={"i": 0, "d": 0.0, "s": "", "b": False,
                             "a": [], "arr": [0, ""]})
    assert got["i"] == {"intValue": "0"}
    assert got["d"] == {"doubleValue": 0.0}
    assert got["s"] == {"stringValue": ""}
    assert got["b"] == {"boolValue": False}
    assert got["a"] == {"arrayValue": {"values": []}}
    assert got["arr"] == {"arrayValue": {"values": [
        {"intValue": "0"}, {"stringValue": ""}]}}


def test_zero_timestamps_and_empty_name_are_still_written():
    span = _one(start_ns=0, end_ns=0, name="")
    assert span["startTimeUnixNano"] == "0"
    assert span["endTimeUnixNano"] == "0"
    assert span["name"] == ""


def test_export_posts_json_with_bearer_key():
    with mock.patch.object(net._OPENER, "open", return_value=_fake_urlopen()) as m:
        otlp.export("https://ingest.example.com", "key", b"{}")
        req = m.call_args[0][0]
        assert req.get_header("Content-type") == "application/json"
        assert req.get_header("Authorization") == "Bearer key"
