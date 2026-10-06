"""OTLP/JSON encoding and export for span trees.

Builds an ExportTraceServiceRequest as OTLP/HTTP JSON, using only the standard
library's json module. The receiver accepts it on the same /v1/traces endpoint
as protobuf (RIUS-1229).

The spec details that matter, each checked against the receiver's decoder:
- traceId, spanId and parentSpanId are lowercase HEX strings, not base64.
- 64-bit integers (timestamps, intValue) are decimal STRINGS, so a value above
  2**53 survives a JSON number round-trip.
- Enums (span kind, status code) are integers.
- An AnyValue holds exactly one member (stringValue, boolValue, intValue,
  doubleValue, arrayValue, bytesValue); the member is written even when it holds
  0, 0.0, "" or false -- an AnyValue with nothing set means "no value".
- bytesValue is base64.
- NaN and the infinities are the strings "NaN", "Infinity", "-Infinity";
  Python's json would otherwise write bare NaN, which is not JSON.
"""
from __future__ import annotations

import base64
import json
import math
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List

from . import net

SCOPE_NAME = "glassflow"
RETRY_DELAY_S = 0.5
CONTENT_TYPE = "application/json"

SPAN_KIND_INTERNAL = 1
_STATUS_CODE = {"OK": 1, "ERROR": 2}
_INT64_MIN = -(1 << 63)
_INT64_MAX = (1 << 63) - 1


def _text(value: str) -> str:
    # A lone surrogate is legal in a Python str but not in UTF-8.
    return value.encode("utf-8", errors="replace").decode("utf-8")


def _double(value: float) -> Any:
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "Infinity" if value > 0 else "-Infinity"
    return value


def _any_value(value: Any) -> Dict[str, Any]:
    # Falling through to str(value) would emit the literal string "None".
    if value is None:
        return {}
    # bool MUST be checked before int -- bool is an int subclass in Python.
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return _int_value(value)
    if isinstance(value, float):
        return {"doubleValue": _double(value)}
    if isinstance(value, (list, tuple)):
        return {"arrayValue": {"values": [_any_value(v) for v in value]}}
    if isinstance(value, (bytes, bytearray)):
        return {"bytesValue": base64.b64encode(bytes(value)).decode("ascii")}
    if isinstance(value, dict):
        return {"stringValue": _text(_json_text(value))}
    return {"stringValue": _text(str(value))}


def _int_value(value: int) -> Dict[str, Any]:
    # Beyond int64 there is no OTLP int that holds it; the digits survive as text.
    if _INT64_MIN <= value <= _INT64_MAX:
        return {"intValue": str(value)}
    return {"stringValue": str(value)}


def _json_text(value: dict) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


def _attributes(attrs: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [{"key": _text(k), "value": _any_value(v)} for k, v in attrs.items()]


def _status(status_code, status_message) -> Dict[str, Any]:
    status: Dict[str, Any] = {}
    if status_message:
        status["message"] = _text(status_message)
    code = _STATUS_CODE.get(status_code, 0)
    if code:
        status["code"] = code
    return status


def _event(time_ns: int, name: str, attrs: Dict[str, Any]) -> Dict[str, Any]:
    event: Dict[str, Any] = {"timeUnixNano": str(time_ns), "name": _text(name)}
    if attrs:
        event["attributes"] = _attributes(attrs)
    return event


def _encode_span(span) -> Dict[str, Any]:
    out: Dict[str, Any] = {"traceId": span.trace_id, "spanId": span.span_id}
    if span.parent_span_id:
        out["parentSpanId"] = span.parent_span_id
    out["name"] = _text(span.name)
    out["kind"] = SPAN_KIND_INTERNAL
    out["startTimeUnixNano"] = str(span.start_ns)
    out["endTimeUnixNano"] = str(span.end_ns)
    if span.attributes:
        out["attributes"] = _attributes(span.attributes)
    events = [_event(*e) for e in getattr(span, "events", None) or []]
    if events:
        out["events"] = events
    status = _status(span.status_code, span.status_message)
    if status:
        out["status"] = status
    return out


def encode(resource_attrs: Dict[str, object], span_list: List[Any]) -> bytes:
    if not span_list:
        return b""
    request = {"resourceSpans": [{
        "resource": {"attributes": _attributes(resource_attrs)},
        "scopeSpans": [{
            "scope": {"name": SCOPE_NAME},
            "spans": [_encode_span(s) for s in span_list],
        }],
    }]}
    return json.dumps(request, ensure_ascii=False, allow_nan=False,
                      separators=(",", ":")).encode("utf-8")


def export(endpoint: str, api_key: str, body: bytes, timeout: float = 5.0,
           retry_delay: float = RETRY_DELAY_S, sleep=time.sleep) -> int:
    """POST to <endpoint>/v1/traces. Returns the HTTP status, or 0 for a
    transport failure or a refused (non-https) URL. One retry, and only for
    5xx/transport. A redirect is not followed: its 3xx is the status.

    The retry waits `retry_delay` first (spec section 9). Retrying instantly
    spends both attempts inside the same instant of an outage -- a receiver
    that is restarting or briefly overloaded is exactly the case the retry
    exists for, and it needs a moment.
    """
    headers = {
        "Content-Type": CONTENT_TYPE,
        "Authorization": "Bearer " + api_key,
    }
    url = endpoint.rstrip("/") + "/v1/traces"
    if not net.is_allowed_url(url):
        return 0                    # no retry can make an http URL safe
    last_status = 0
    for attempt in range(2):
        if attempt:
            sleep(retry_delay)
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with net.urlopen(req, timeout=timeout) as resp:
                status = resp.getcode()
        except urllib.error.HTTPError as exc:
            status = exc.code
        except Exception:
            status = 0

        last_status = status
        if status and status < 500:
            return status

    return last_status
