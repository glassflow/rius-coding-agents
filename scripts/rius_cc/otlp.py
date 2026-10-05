"""OTLP protobuf encoding and export for span trees.

Builds an ExportTraceServiceRequest by hand, using only the wire-format
primitives in proto.py. No protobuf runtime dependency -- see proto.py's
docstring for why. Field numbers and wire types are verified ground truth
(see task-6 brief); the round-trip test decodes our bytes with the real
opentelemetry-proto library to guarantee correctness.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List

from . import proto

SCOPE_NAME = "glassflow"
RETRY_DELAY_S = 0.5


def _any_value(value: Any) -> bytes:
    """AnyValue.value is a oneof, so the member is written even when it holds
    0, 0.0 or "" -- an AnyValue with nothing set means "no value"."""
    # Falling through to str(value) would emit the literal string "None".
    if value is None:
        return b""
    # bool MUST be checked before int -- bool is an int subclass in Python.
    if isinstance(value, bool):
        return proto.tag(2, proto.WIRE_VARINT) + proto.varint(1 if value else 0)
    if isinstance(value, int):
        return _int_value(value)
    if isinstance(value, float):
        return proto.double_member(4, value)
    if isinstance(value, (list, tuple)):
        inner = b"".join(proto.ld(1, _any_value(v)) for v in value)
        return proto.ld(5, inner)
    if isinstance(value, (bytes, bytearray)):
        return proto.ld(7, bytes(value))
    if isinstance(value, dict):
        return proto.string_member(1, _json_text(value))
    return proto.string_member(1, str(value))


def _int_value(value: int) -> bytes:
    # Beyond int64 there is no OTLP int that holds it; the digits survive as text.
    if proto.INT64_MIN <= value <= proto.INT64_MAX:
        return proto.int64_member(3, value)
    return proto.string_member(1, str(value))


def _json_text(value: dict) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


def _attributes(field: int, attrs: Dict[str, Any]) -> bytes:
    out = bytearray()
    for k, v in attrs.items():
        payload = proto.string_field(1, k) + proto.ld(2, _any_value(v))
        out += proto.ld(field, payload)
    return bytes(out)


_STATUS_CODE = {"OK": 1, "ERROR": 2}


def _status(status_code, status_message) -> bytes:
    if status_code is None and not status_message:
        return b""
    payload = b""
    if status_message:
        payload += proto.string_field(2, status_message)
    code = _STATUS_CODE.get(status_code, 0)
    if code:
        payload += proto.varint_field(3, code)
    if not payload:
        return b""
    return proto.ld(15, payload)


def _event(time_ns: int, name: str, attrs: Dict[str, Any]) -> bytes:
    """Span.Event: time_unix_nano=1 (fixed64), name=2, attributes=3."""
    payload = proto.fixed64_field(1, time_ns)
    payload += proto.string_field(2, name)
    payload += _attributes(3, attrs)
    return proto.ld(11, payload)


def _encode_span(span) -> bytes:
    payload = b""
    payload += proto.bytes_field(1, bytes.fromhex(span.trace_id))
    payload += proto.bytes_field(2, bytes.fromhex(span.span_id))
    if span.parent_span_id:
        payload += proto.bytes_field(4, bytes.fromhex(span.parent_span_id))
    payload += proto.string_field(5, span.name)
    payload += proto.tag(6, proto.WIRE_VARINT) + proto.varint(1)  # SpanKind.INTERNAL
    payload += proto.fixed64_field(7, span.start_ns)
    payload += proto.fixed64_field(8, span.end_ns)
    payload += _attributes(9, span.attributes)
    for time_ns, name, attrs in getattr(span, "events", None) or []:
        payload += _event(time_ns, name, attrs)
    payload += _status(span.status_code, span.status_message)
    return proto.ld(2, payload)


def encode(resource_attrs: Dict[str, object], span_list: List[Any]) -> bytes:
    if not span_list:
        return b""

    scope_payload = proto.string_field(1, SCOPE_NAME)
    spans_bytes = b"".join(_encode_span(s) for s in span_list)
    scope_spans_payload = proto.ld(1, scope_payload) + spans_bytes
    scope_spans = proto.ld(2, scope_spans_payload)

    resource_payload = _attributes(1, resource_attrs)
    resource = proto.ld(1, resource_payload)

    resource_spans_payload = resource + scope_spans
    resource_spans = proto.ld(1, resource_spans_payload)

    return resource_spans


def export(endpoint: str, api_key: str, body: bytes, timeout: float = 5.0,
           retry_delay: float = RETRY_DELAY_S, sleep=time.sleep) -> int:
    """POST to <endpoint>/v1/traces. Returns the HTTP status, or 0 for a
    transport failure. One retry, and only for 5xx/transport.

    The retry waits `retry_delay` first (spec section 9). Retrying instantly
    spends both attempts inside the same instant of an outage -- a receiver
    that is restarting or briefly overloaded is exactly the case the retry
    exists for, and it needs a moment.
    """
    headers = {
        "Content-Type": "application/x-protobuf",
        "Authorization": "Bearer " + api_key,
    }
    url = endpoint.rstrip("/") + "/v1/traces"
    last_status = 0
    for attempt in range(2):
        if attempt:
            sleep(retry_delay)
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                status = resp.getcode()
        except urllib.error.HTTPError as exc:
            status = exc.code
        except Exception:
            status = 0

        last_status = status
        if status and status < 500:
            return status

    return last_status
