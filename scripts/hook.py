#!/usr/bin/env python3
"""Claude Code hook entry point. Runs in the session's critical path.

Does as little as possible: resolve config, and if enabled, hand off to a
DETACHED exporter and exit. Never writes to stdout -- stdout is a control
channel for hooks. Never exits non-zero -- instrumentation that can break the
session it observes is worse than no instrumentation.
"""
import json
import os
import subprocess
import sys
import tempfile


def main() -> None:
    try:
        event = sys.argv[1] if len(sys.argv) > 1 else ""
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict) or not payload.get("session_id"):
            return

        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from rius_cc import config

        home = os.path.expanduser("~")
        cfg = config.resolve(payload.get("session_id", ""),
                             payload.get("cwd", ""), os.environ, home)
        if not cfg.enabled:
            return

        fd, path = tempfile.mkstemp(prefix="rius-hook-", suffix=".json")
        with os.fdopen(fd, "w") as fh:
            json.dump({"event": event, "payload": payload}, fh)

        exporter = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "exporter.py")
        subprocess.Popen(
            [sys.executable, exporter, path],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,      # detach: outlives this process
            close_fds=True,
        )
    except BaseException:
        pass


if __name__ == "__main__":
    main()
    sys.exit(0)
