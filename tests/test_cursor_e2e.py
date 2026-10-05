"""A whole Cursor session through the real launcher: hook.sh --agent cursor
-> spool -> detached exporter -> HTTP POST to a local receiver, decoded with
opentelemetry-proto. The payloads are the docs-derived fixture, not a live
Cursor run."""
import json
import pathlib
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from tests import cursor_fixtures
from tests.platforms import BASH, minimal_env, posix_only

pytest.importorskip("opentelemetry.proto.trace.v1.trace_pb2")
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (  # noqa: E402
    ExportTraceServiceRequest,
)

ROOT = pathlib.Path(__file__).parent.parent
LAUNCHER = str(ROOT / "scripts" / "hook.sh")
WIRED = set(json.loads((ROOT / "cursor" / "hooks.json").read_text())["hooks"])


class Receiver(BaseHTTPRequestHandler):
    bodies = []

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        Receiver.bodies.append((self.headers.get("Authorization"), body))
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *a):
        pass


@pytest.fixture
def server():
    Receiver.bodies = []
    srv = HTTPServer(("127.0.0.1", 0), Receiver)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield "http://127.0.0.1:%d" % srv.server_address[1]
    srv.shutdown()


def _attrs(span):
    out = {}
    for kv in span.attributes:
        kind = kv.value.WhichOneof("value")
        out[kv.key] = getattr(kv.value, kind) if kind else None
    return out


def _latest_spans():
    """Each span's last version, as the backend keeps it."""
    latest = {}
    for _, body in Receiver.bodies:
        req = ExportTraceServiceRequest()
        req.ParseFromString(body)
        for rs in req.resource_spans:
            resource = _attrs(rs.resource)
            for span in rs.scope_spans[0].spans:
                prev = latest.get(span.span_id)
                if prev is None or not _attrs(span).get("glassflow.span.pending"):
                    latest[span.span_id] = (span, resource)
    return latest


def _wait_for_closed_root(timeout_s=20):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        for span, _ in _latest_spans().values():
            if not span.parent_span_id and not _attrs(span).get(
                    "glassflow.span.pending"):
                return
        time.sleep(0.1)
    raise AssertionError("the closed root never arrived")


@posix_only("Cursor hooks run the launcher through bash")
def test_a_cursor_session_reaches_the_receiver_as_one_tree(server, tmp_path):
    env = minimal_env(HOME=str(tmp_path), PATH="/usr/bin:/bin:/usr/local/bin",
                      RIUS_API_KEY="glassflow_dummy", RIUS_ENDPOINT=server,
                      RIUS_CURSOR_ENABLED="true")
    for payload in cursor_fixtures.payloads("docs_session"):
        event = payload["hook_event_name"]
        if event not in WIRED:
            continue
        r = subprocess.run([BASH, LAUNCHER, "--agent", "cursor", event],
                           input=json.dumps(payload), capture_output=True,
                           text=True, env=env, timeout=30)
        assert r.returncode == 0 and isinstance(json.loads(r.stdout), dict)
    _wait_for_closed_root()

    assert {auth for auth, _ in Receiver.bodies} == {"Bearer glassflow_dummy"}
    latest = _latest_spans()
    spans = {span.name: span for span, _ in latest.values()}
    resource = next(iter(latest.values()))[1]
    assert resource["service.name"] == "cursor"
    assert len({span.trace_id for span in spans.values()}) == 1
    assert not any(_attrs(s).get("glassflow.span.pending")
                   for s in spans.values())

    root = spans["cursor session"]
    assert sum(1 for s, _ in latest.values() if s.name == "turn") == 2
    for tool in ("Shell", "Read", "MCP:list_traces", "Write", "Task"):
        assert spans[tool].parent_span_id != root.span_id
    assert _attrs(spans["Shell"])["error.type"] == "Shell.exit_1"
    assert _attrs(spans["MCP:list_traces"])["error.type"] == "MCP:list_traces.timeout"
    subagent = next(s for s, _ in latest.values()
                    if _attrs(s).get("cursor.subagent.id"))
    assert subagent.parent_span_id == spans["Task"].span_id
    assert spans["Grep"].parent_span_id == subagent.span_id
    assert not any(k.startswith("gen_ai.usage") for s in spans.values()
                   for k in _attrs(s))
    assert not (tmp_path / ".claude").exists()
