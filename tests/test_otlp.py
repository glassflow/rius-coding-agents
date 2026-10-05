import struct
from unittest import mock

from rius_cc import net, otlp, proto, spans


def _span(**kw):
    s = spans.Span(
        trace_id="a" * 32, span_id="b" * 16, parent_span_id=None,
        name="n", kind_oi="LLM", start_ns=1_000_000_000, end_ns=2_000_000_000,
        attributes={}, status_code=None, status_message=None, pending=False)
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def test_ids_are_raw_bytes_not_hex_text():
    body = otlp.encode({}, [_span()])
    assert bytes.fromhex("a" * 32) in body
    assert b"a" * 32 not in body        # the hex TEXT must never appear


def test_timestamps_are_fixed64_little_endian():
    body = otlp.encode({}, [_span(start_ns=1758535200000000000)])
    assert b"\x39" + struct.pack("<Q", 1758535200000000000) in body


def test_scope_name_is_glassflow():
    assert otlp.SCOPE_NAME == "glassflow"
    assert b"glassflow" in otlp.encode({}, [_span()])


def test_root_span_omits_parent_field():
    """`b"\\x22" not in body` was a raw byte scan for a value that is also the
    ASCII double quote, so it would have failed the moment any name or
    attribute contained one. Compare the two encodings instead."""
    parent = "c" * 16
    without = otlp.encode({}, [_span(parent_span_id=None, name='say "hi"')])
    with_parent = otlp.encode({}, [_span(parent_span_id=parent, name='say "hi"')])

    assert bytes.fromhex(parent) not in without
    assert bytes.fromhex(parent) in with_parent
    # field 4, LEN, 8 bytes of span id: tag + length + payload
    assert proto.bytes_field(4, bytes.fromhex(parent)) in with_parent
    assert len(with_parent) == len(without) + 10


def test_attribute_types_encode_distinctly():
    """The old version asserted only that two substrings appeared, so it
    would have passed with every value encoded as a string."""
    body = otlp.encode({}, [_span(attributes={
        "s": "txt", "i": 7, "b": True, "d": 1.5, "arr": ["x", "y"]})])

    # AnyValue field 1 = string, 2 = bool (varint), 3 = int, 4 = double,
    # 5 = array. Each value must use its OWN field, not field 1.
    assert proto.string_field(1, "txt") in body
    assert proto.tag(2, proto.WIRE_VARINT) + proto.varint(1) in body
    assert proto.varint_field(3, 7) in body
    assert proto.double_field(4, 1.5) in body
    assert proto.ld(5, proto.ld(1, proto.string_field(1, "x"))
                    + proto.ld(1, proto.string_field(1, "y"))) in body
    # the give-away that a value got stringified
    for stringified in (b"True", b"true", b"1.5", b"'x'"):
        assert proto.string_field(1, stringified.decode()) not in body


def test_a_none_attribute_is_an_empty_any_value_not_the_word_none():
    body = otlp.encode({}, [_span(attributes={"k": None})])
    assert b"None" not in body
    # key present, value submessage present and empty
    assert proto.string_field(1, "k") + proto.ld(2, b"") in body


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


def test_zero_valued_attributes_still_carry_their_type():
    """Without the round-trip library: each zero must still write its AnyValue
    member, so the KeyValue's value is not an empty AnyValue."""
    for value, member in [(0, proto.int64_member(3, 0)),
                          (0.0, proto.double_member(4, 0.0)),
                          ("", proto.string_member(1, ""))]:
        body = otlp.encode({}, [_span(attributes={"z": value})])
        assert proto.string_field(1, "z") + proto.ld(2, member) in body
