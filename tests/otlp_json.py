"""Decode our OTLP/JSON body into the REAL opentelemetry-proto messages.

Test-only. protobuf's own json_format reads every bytes field as base64, but
OTLP/JSON spells trace and span ids as hex, so ids are converted first -- the
same trick the receiver's decoder (the Collector's pdata) is built around. Any
other deviation from the OTLP/JSON spec makes ParseDict raise.
"""
import base64
import json
import re

import pytest

pytest.importorskip("opentelemetry.proto.trace.v1.trace_pb2")

from google.protobuf import json_format  # noqa: E402
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (  # noqa: E402
    ExportTraceServiceRequest,
)

_ID_LENGTHS = {"traceId": 32, "spanId": 16, "parentSpanId": 16}


def _hex_ids_to_base64(node):
    if isinstance(node, dict):
        for key, child in node.items():
            if key in _ID_LENGTHS:
                assert isinstance(child, str), (key, child)
                assert re.fullmatch("[0-9a-f]{%d}" % _ID_LENGTHS[key], child), (key, child)
                node[key] = base64.b64encode(bytes.fromhex(child)).decode("ascii")
            else:
                _hex_ids_to_base64(child)
    elif isinstance(node, list):
        for child in node:
            _hex_ids_to_base64(child)


def to_request(body):
    """The ExportTraceServiceRequest a spec-following receiver would read."""
    tree = json.loads(body.decode("utf-8"))
    _hex_ids_to_base64(tree)
    req = ExportTraceServiceRequest()
    json_format.ParseDict(tree, req)
    return req
