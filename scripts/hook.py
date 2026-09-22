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
        from rius_cc import config, state

        home = os.path.expanduser("~")
        session_id = payload.get("session_id", "")
        cwd = payload.get("cwd", "")
        cfg = config.resolve(session_id, cwd, os.environ, home)
        if not cfg.enabled:
            return

        script_dir = os.path.dirname(os.path.abspath(__file__))

        fd, path = tempfile.mkstemp(prefix="rius-hook-", suffix=".json")
        with os.fdopen(fd, "w") as fh:
            json.dump({"event": event, "payload": payload}, fh)

        exporter = os.path.join(script_dir, "exporter.py")
        subprocess.Popen(
            [sys.executable, exporter, path],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,      # detach: outlives this process
            close_fds=True,
        )

        if event == "SessionStart":
            # The heartbeat pinger watches this process's parent (the live
            # Claude Code process that invoked this hook), not this
            # short-lived hook process itself.
            heartbeat = os.path.join(script_dir, "heartbeat.py")
            subprocess.Popen(
                [sys.executable, heartbeat, session_id, cwd, home,
                 str(_claude_code_pid())],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
            )
        elif event == "SessionEnd":
            # Tell any running pinger for this session to send its final
            # stopped ping and exit. Best-effort, non-blocking.
            try:
                stop_path = os.path.join(state.state_dir(home),
                                         session_id + ".heartbeat.stop")
                with open(stop_path, "w") as fh:
                    fh.write("")
            except OSError:
                pass
    except BaseException:
        pass


def _claude_code_pid() -> int:
    """Best-effort pid of the live Claude Code process for this session.

    hooks.json invokes this script through `bash -c "... hook.py EVENT"`, so
    this process's immediate parent is that short-lived bash, not Claude
    Code -- bash exits right after hook.py does, regardless of whether
    Claude Code itself is still alive. Walk up one more level (bash's
    parent) via `ps`, which is Claude Code's actual pid. Falls back to the
    immediate parent if that lookup fails for any reason.
    """
    ppid = os.getppid()
    try:
        out = subprocess.check_output(
            ["ps", "-o", "ppid=", "-p", str(ppid)],
            stderr=subprocess.DEVNULL, timeout=1,
        )
        grandparent = int(out.decode().strip())
        if grandparent > 0:
            return grandparent
    except BaseException:
        pass
    return ppid


if __name__ == "__main__":
    main()
    sys.exit(0)
