"""Full path: hook -> detached exporter -> HTTP POST, against a real socket.

A fake OTLP receiver on localhost, decoding with opentelemetry-proto. This is
the only test that exercises process detachment, so it is the only one that
would catch a hook that spawns an exporter which never runs.
"""
import json
import pathlib
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

pytest.importorskip("opentelemetry.proto.trace.v1.trace_pb2")
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (  # noqa: E402
    ExportTraceServiceRequest,
)

ROOT = pathlib.Path(__file__).parent.parent
HOOK = str(ROOT / "scripts" / "hook.py")

received = []


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        received.append({"path": self.path,
                         "auth": self.headers.get("Authorization"),
                         "ctype": self.headers.get("Content-Type"),
                         "body": body})
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *a):
        pass


@pytest.fixture
def server():
    received.clear()
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    # config.resolve treats RIUS_ENDPOINT as the exact URL posted to (see
    # rius_cc/otlp.py export() and config.py) -- there is no path-joining
    # in the shipped code, so the fixture endpoint must already include
    # /v1/traces. The brief's literal `server` string (bare origin, no
    # path) would make every request land on "/", not "/v1/traces", and
    # the path assertion below would fail. This is the one deviation from
    # the brief's literal test code.
    yield "http://127.0.0.1:%d/v1/traces" % srv.server_address[1]
    srv.shutdown()


def test_hook_to_receiver_full_path(server, tmp_path, fixtures_dir):
    home = tmp_path / "home"
    (home / ".claude" / "rius").mkdir(parents=True)
    with open(home / ".claude" / "rius" / "config.json", "w") as fh:
        json.dump({"enabled_paths": ["/tmp"]}, fh)

    payload = {
        "session_id": "22222222-2222-2222-2222-222222222222",
        "transcript_path": str(fixtures_dir / "tool_call.jsonl"),
        "cwd": "/tmp/proj",
        "hook_event_name": "PostToolUse",
    }
    env = {
        "HOME": str(home), "PATH": "/usr/bin:/bin",
        "RIUS_API_KEY": "glassflow_testkey",
        "RIUS_ENDPOINT": server,
    }
    r = subprocess.run([sys.executable, HOOK, "PostToolUse"],
                       input=json.dumps(payload), capture_output=True,
                       text=True, env=env, timeout=30)
    assert r.returncode == 0

    deadline = time.time() + 15
    while not received and time.time() < deadline:
        time.sleep(0.05)
    assert received, "detached exporter never POSTed"

    got = received[0]
    assert got["path"] == "/v1/traces"
    assert got["auth"] == "Bearer glassflow_testkey"
    assert got["ctype"] == "application/x-protobuf"

    req = ExportTraceServiceRequest()
    req.ParseFromString(got["body"])
    spans_out = req.resource_spans[0].scope_spans[0].spans
    assert req.resource_spans[0].scope_spans[0].scope.name == "glassflow"

    kinds = {}
    for s in spans_out:
        attrs = {kv.key: kv.value.string_value for kv in s.attributes}
        kinds.setdefault(attrs.get("openinference.span.kind"), []).append(s)
    assert "AGENT" in kinds        # session root
    assert "LLM" in kinds          # a generation
    assert "TOOL" in kinds         # the Read call

    root = kinds["AGENT"][0]
    assert root.parent_span_id == b""
    assert len({s.trace_id for s in spans_out}) == 1     # one trace per session


def test_disabled_folder_posts_nothing(server, tmp_path, fixtures_dir):
    home = tmp_path / "home"
    (home / ".claude" / "rius").mkdir(parents=True)
    payload = {"session_id": "s2",
               "transcript_path": str(fixtures_dir / "simple.jsonl"),
               "cwd": "/somewhere/else", "hook_event_name": "PostToolUse"}
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin",
           "RIUS_API_KEY": "glassflow_testkey", "RIUS_ENDPOINT": server}
    subprocess.run([sys.executable, HOOK, "PostToolUse"],
                   input=json.dumps(payload), capture_output=True,
                   text=True, env=env, timeout=30)
    time.sleep(2)
    assert received == []
