"""A local OTLP/HTTP receiver for the live harness.

Answers 200 to every POST on /v1/traces and appends each body, decoded, as one
line of <run dir>/spans.jsonl. Run alone: `python receiver.py <run dir>`.
"""
from __future__ import annotations

import gzip
import http.server
import json
import pathlib
import sys
import threading

HOST = "127.0.0.1"
PORT = 4318
TRACES_PATH = "/v1/traces"


class PortBusy(Exception):
    pass


class _Server(http.server.ThreadingHTTPServer):
    allow_reuse_address = sys.platform != "win32"    # there it would share a busy port


class Receiver:
    def __init__(self, run_dir, port=PORT):
        self.path = pathlib.Path(run_dir) / "spans.jsonl"
        self.requests = 0
        self._lock = threading.Lock()
        try:
            self._server = _Server((HOST, port), self._handler())
        except OSError as exc:
            raise PortBusy("%s:%d is in use (an OTel collector?): %s" % (HOST, port, exc))
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def start(self):
        self.path.touch()
        self._thread.start()

    def stop(self):
        self._server.shutdown()
        self._server.server_close()

    def lines(self):
        with self._lock:
            return self.path.read_text(encoding="utf-8").splitlines()

    def _append(self, body):
        with self._lock:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(body, separators=(",", ":")) + "\n")
            self.requests += 1

    def _handler(self):
        receiver = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if self.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                if self.path != TRACES_PATH:
                    return self._reply(404)
                try:
                    receiver._append(json.loads(raw))
                except ValueError:
                    return self._reply(400)
                self._reply(200)

            def _reply(self, status):
                body = b"{}"
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        return Handler


def main(argv):
    if len(argv) != 2:
        print("usage: receiver.py <run dir>", file=sys.stderr)
        return 2
    pathlib.Path(argv[1]).mkdir(parents=True, exist_ok=True)
    try:
        receiver = Receiver(argv[1])
    except PortBusy as exc:
        print("error:", exc, file=sys.stderr)
        return 1
    receiver.start()
    print("listening on http://%s:%d, writing %s" % (HOST, PORT, receiver.path))
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        receiver.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
