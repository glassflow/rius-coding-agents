import struct

from rius_cc import otlp, spans


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
    body = otlp.encode({}, [_span(parent_span_id=None)])
    assert b"\x22" not in body          # tag for field 4, parent_span_id


def test_attribute_types_encode_distinctly():
    body = otlp.encode({}, [_span(attributes={
        "s": "txt", "i": 7, "b": True, "d": 1.5, "arr": ["x", "y"]})])
    assert b"txt" in body and b"arr" in body


def test_empty_span_list_is_empty_body():
    assert otlp.encode({}, []) == b""
