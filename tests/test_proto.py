import struct

import pytest

from rius_cc import proto


def test_varint_single_byte():
    assert proto.varint(0) == b"\x00"
    assert proto.varint(1) == b"\x01"
    assert proto.varint(127) == b"\x7f"


def test_varint_multi_byte():
    assert proto.varint(128) == b"\x80\x01"
    assert proto.varint(300) == b"\xac\x02"


def test_tag_is_field_shifted_plus_wire_type():
    assert proto.tag(1, 2) == b"\x0a"      # TracesData.resource_spans
    assert proto.tag(7, 1) == b"\x39"      # Span.start_time_unix_nano, fixed64
    assert proto.tag(16, 5) == b"\x85\x01" # Span.flags, two-byte tag


def test_string_field_omits_proto3_default():
    assert proto.string_field(3, "") == b""
    assert proto.string_field(5, "ab") == b"\x2a\x02ab"


def test_varint_field_omits_zero():
    assert proto.varint_field(6, 0) == b""
    assert proto.varint_field(6, 1) == b"\x30\x01"


def test_fixed64_is_little_endian_eight_bytes():
    ns = 1758573545117000000
    assert proto.fixed64_field(7, ns) == b"\x39" + struct.pack("<Q", ns)


def test_fixed64_omits_zero():
    assert proto.fixed64_field(7, 0) == b""


def test_bytes_field_omits_empty():
    assert proto.bytes_field(4, b"") == b""
    assert proto.bytes_field(2, b"\x01" * 8) == b"\x12\x08" + b"\x01" * 8


def test_ld_always_emits_even_when_empty():
    # an empty submessage is presence, not a default
    assert proto.ld(15, b"") == b"\x7a\x00"
