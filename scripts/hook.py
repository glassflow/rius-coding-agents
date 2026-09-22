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
import uuid

# Bounded wait for the session lock when minting the instance id. Only ever
# paid on SessionStart, where contention is near-impossible; it exists so the
# mint can never interleave with a concurrent exporter's read-modify-write.
MINT_LOCK_TIMEOUT_S = 1.0


def _mint_instance_id(state, session_id: str, home: str) -> str:
    """Return this session's instance id, minting and persisting it if new.

    This MUST happen before either child is spawned. The heartbeat pinger
    cannot invent an instance id -- it has to match the one the spans carry --
    so if it starts before the id exists it exits immediately and the session
    produces no heartbeats at all, silently, forever.
    """
    with state.session_lock(session_id, home,
                            block_timeout=MINT_LOCK_TIMEOUT_S):
        st = state.load(session_id, home)
        instance_id = st.get("instance_id")
        if instance_id:
            return instance_id
        instance_id = str(uuid.uuid4())
        st["instance_id"] = instance_id
        state.save(session_id, home, st)
        return instance_id


def _clear_stop_file(state, session_id: str, home: str) -> None:
    """Delete any stop file left behind by a previous run of this session id.

    SessionEnd writes it and nothing else removes it; on resume or /clear the
    fresh pinger would find it on its first iteration and report a live
    session as stopped.
    """
    try:
        os.remove(os.path.join(state.state_dir(home),
                               session_id + ".heartbeat.stop"))
    except OSError:
        pass


def _spawn_stderr(cfg, home: str):
    """Where a detached child's stderr goes.

    DEVNULL hides any failure that happens BEFORE the child's own try/except
    (a bad interpreter, an ImportError), so with debug on it goes to the same
    log directory as everything else. Returns (stderr_target, file_or_None).
    """
    if not getattr(cfg, "debug", False):
        return subprocess.DEVNULL, None
    try:
        log_dir = os.path.join(home, ".claude", "rius", "log")
        os.makedirs(log_dir, exist_ok=True)
        fh = open(os.path.join(log_dir, "spawn.log"), "a")
        return fh, fh
    except OSError:
        return subprocess.DEVNULL, None


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

        # Mint BEFORE anything is spawned, so both children are handed the
        # same id and neither has to race the other for it.
        instance_id = ""
        if event == "SessionStart":
            instance_id = _mint_instance_id(state, session_id, home)
            _clear_stop_file(state, session_id, home)

        fd, path = tempfile.mkstemp(prefix="rius-hook-", suffix=".json")
        with os.fdopen(fd, "w") as fh:
            json.dump({"event": event, "payload": payload}, fh)

        stderr, log_fh = _spawn_stderr(cfg, home)
        try:
            exporter = os.path.join(script_dir, "exporter.py")
            subprocess.Popen(
                [sys.executable, exporter, path, instance_id],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=stderr,
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
                     str(_claude_code_pid()), instance_id],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=stderr,
                    start_new_session=True,
                    close_fds=True,
                )
        finally:
            if log_fh is not None:
                log_fh.close()

        if event == "SessionEnd":
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
