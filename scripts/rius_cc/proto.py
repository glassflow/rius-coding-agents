"""Minimal protobuf wire-format encoder.

Encode-only, for a fixed schema (OTLP TracesData). No parsing, no unknown
fields, no schema evolution -- which is what keeps this small enough to own.

Proto3 default omission is deliberate: emitting a singular scalar equal to its
default is valid wire format, but omitting it is what makes our output
byte-identical to the reference serializer, and that equality is the property
tests/test_otlp_roundtrip.py checks.

A oneof member is the exception: once set it is always serialised, even when
it equals the default, because presence is what says which member is set. The
*_member functions below are for oneof members and never omit.
"""
from __future__ import annotations

import struct

WIRE_VARINT = 0
WIRE_FIXED64 = 1
WIRE_LEN = 2
WIRE_FIXED32 = 5


INT64_MIN = -(1 << 63)
INT64_MAX = (1 << 63) - 1
_UINT64_MASK = (1 << 64) - 1


def varint(value: int) -> bytes:
    """Base-128, least-significant group first, MSB set on continuation."""
    if value < 0:
        raise ValueError("varint takes an unsigned value; mask int64 first")
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def tag(field: int, wire: int) -> bytes:
    return varint((field << 3) | wire)


def ld(field: int, payload: bytes) -> bytes:
    """Length-delimited. Always emits -- an empty submessage means presence."""
    return tag(field, WIRE_LEN) + varint(len(payload)) + payload


def _utf8(text: str) -> bytes:
    # A lone surrogate is legal in a Python str but not in UTF-8.
    return text.encode("utf-8", errors="replace")


def string_field(field: int, text: str) -> bytes:
    if not text:
        return b""
    return string_member(field, text)


def string_member(field: int, text: str) -> bytes:
    return ld(field, _utf8(text))


def bytes_field(field: int, raw: bytes) -> bytes:
    if not raw:
        return b""
    return ld(field, raw)


def varint_field(field: int, value: int) -> bytes:
    if not value:
        return b""
    return int64_member(field, value)


def int64_member(field: int, value: int) -> bytes:
    if not INT64_MIN <= value <= INT64_MAX:
        raise ValueError("value outside int64: %d" % value)
    # int64 negatives are two's complement as unsigned 64-bit (not zigzag --
    # zigzag is sint32/sint64 only, which appear nowhere in OTLP).
    return tag(field, WIRE_VARINT) + varint(value & _UINT64_MASK)


def fixed64_field(field: int, value: int) -> bytes:
    if not value:
        return b""
    return tag(field, WIRE_FIXED64) + struct.pack("<Q", value)


def double_field(field: int, value: float) -> bytes:
    if value == 0:
        return b""
    return double_member(field, value)


def double_member(field: int, value: float) -> bytes:
    return tag(field, WIRE_FIXED64) + struct.pack("<d", value)
