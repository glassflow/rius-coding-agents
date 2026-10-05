# No shebang, deliberately. This file is only ever run as an
# ARGUMENT to an interpreter -- scripts/hook.sh execs it, and the
# test suite uses sys.executable. A `#!/usr/bin/env python3` line
# here is not merely unused on Windows: the `py` launcher honours
# it by PATH-searching for `python3` BEFORE consulting its own
# registered interpreters, and the only `python3` on a default
# Windows PATH is the Microsoft Store alias -- the exact stub
# hook.sh picks `py` to avoid. See scripts/_find_python.sh.
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


def _mint_instance_id(state, session_id: str, home: str, source: str = "",
                      cc_pid: int = 0) -> str:
    """Return this session's instance id, minting and persisting it if new.

    This MUST happen before either child is spawned. The heartbeat pinger
    cannot invent an instance id -- it has to match the one the spans carry --
    so if it starts before the id exists it exits immediately and the session
    produces no heartbeats at all, silently, forever.

    `source` distinguishes why SessionStart fired (per the hook payload):
    a "resume" is a NEW process lifetime -- the old instance id already had
    its `stopped: true` ping sent by the prior SessionEnd, so reusing it
    would make the backend see a stopped instance start pinging again. A
    fresh id is minted and replaces whatever is in state. "compact" and
    "clear" are the SAME process continuing (with a compacted context, or a
    cleared transcript) and must keep the existing id. "startup" or an
    absent source keeps today's behaviour: mint only if none exists yet.

    `cc_pid`, the Claude Code process, is recorded alongside: while it is
    alive, no other session's sweep closes this one's trace.
    """
    with state.session_lock(session_id, home,
                            block_timeout=MINT_LOCK_TIMEOUT_S):
        st = state.load(session_id, home)
        instance_id = st.get("instance_id")
        if not instance_id or source == "resume":
            instance_id = str(uuid.uuid4())
            st["instance_id"] = instance_id
        elif st.get("cc_pid") == cc_pid:
            return instance_id
        st["cc_pid"] = cc_pid
        state.save(session_id, home, st)
        return instance_id


def _clear_stop_file(state, session_id: str, home: str) -> None:
    """Delete any stop file left behind by a previous run of this session id.

    SessionEnd writes it and nothing else removes it; on resume or /clear the
    fresh pinger would find it on its first iteration and report a live
    session as stopped.
    """
    try:
        os.remove(state.session_file(session_id, home, ".heartbeat.stop"))
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
        from rius_cc import platform_compat
        log_dir = platform_compat.rius_dir(home, "log")
        fh = platform_compat.open_private_append(os.path.join(log_dir, "spawn.log"))
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
        from rius_cc import config, continuation, platform_compat, state

        session_id = payload.get("session_id", "")
        if not state.is_valid_session_id(session_id):
            return
        home = platform_compat.home_dir(os.environ)
        cwd = payload.get("cwd", "")
        cfg = config.resolve(session_id, cwd, os.environ, home)
        if event == "SessionStart" and cfg.api_key:
            # A conversation Claude Code moved to this new id is taken over
            # HERE, before anything is spawned: the old id's pinger is told
            # to stop before this id's starts (one pinger per root), and the
            # gate below sees the conversation's open trace even when this
            # folder is disabled.
            try:
                continuation.link(session_id, payload.get("transcript_path", ""),
                                  home)
            except Exception:
                pass
        # A session disabled after its trace started still needs the
        # exporter: it records the stop, and on SessionEnd closes the trace.
        # So does a conversation that brought its stop with it.
        stopped = not cfg.enabled or (
            event == "SessionStart"
            and bool(state.load(session_id, home).get("continued_from"))
            and bool(state.load(session_id, home).get("content_stopped")))
        if stopped and not (cfg.api_key
                            and state.trace_is_open(state.load(session_id, home))):
            return

        script_dir = os.path.dirname(os.path.abspath(__file__))

        # Mint BEFORE anything is spawned, so both children are handed the
        # same id and neither has to race the other for it.
        instance_id = ""
        cc_pid = 0
        if event == "SessionStart" and not stopped:
            source = payload.get("source", "")
            cc_pid = _claude_code_pid()
            instance_id = _mint_instance_id(state, session_id, home, source,
                                            cc_pid)
            _clear_stop_file(state, session_id, home)

        stderr, log_fh = _spawn_stderr(cfg, home)
        # detach: the child must outlive this hook process. setsid on POSIX,
        # DETACHED_PROCESS|CREATE_NEW_PROCESS_GROUP on Windows.
        detach = platform_compat.detached_child_kwargs()
        try:
            _spawn_exporter(script_dir, event, payload, instance_id,
                            stderr, detach)

            if event == "SessionStart" and not stopped:
                # The heartbeat pinger watches this process's parent (the live
                # Claude Code process that invoked this hook), not this
                # short-lived hook process itself.
                heartbeat = os.path.join(script_dir, "heartbeat.py")
                subprocess.Popen(
                    [sys.executable, heartbeat, session_id, cwd, home,
                     str(cc_pid), instance_id],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=stderr,
                    close_fds=True,
                    **detach
                )
        finally:
            if log_fh is not None:
                log_fh.close()

        if event == "SessionEnd":
            # Tell any running pinger for this session to send its final
            # stopped ping and exit. Best-effort, non-blocking.
            try:
                stop_path = state.session_file(session_id, home,
                                               ".heartbeat.stop")
                with open(stop_path, "w") as fh:
                    fh.write("")
            except OSError:
                pass
    except BaseException:
        pass


def _spawn_exporter(script_dir, event, payload, instance_id, stderr, detach):
    """Hand the payload to a detached exporter, which deletes the file once
    it has read it. The payload holds prompt text and tool output, so if
    the exporter never starts the file goes now instead of lingering."""
    fd, path = tempfile.mkstemp(prefix="rius-hook-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump({"event": event, "payload": payload}, fh)
        subprocess.Popen(
            [sys.executable, os.path.join(script_dir, "exporter.py"), path,
             instance_id],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=stderr,
            close_fds=True,
            **detach
        )
    except BaseException:
        try:
            os.remove(path)
        except OSError:
            pass
        raise


def _claude_code_pid() -> int:
    """Best-effort pid of the live Claude Code process, or 0 if unknown.

    hooks.json invokes this script through a shell, so this process's
    immediate parent is that short-lived shell, not Claude Code -- it exits
    right after hook.py does, regardless of whether Claude Code is still
    alive. Walk up one more level; the grandparent is Claude Code's actual
    pid. `rius_cc.platform_compat` does the walk: `ps` on POSIX, a Toolhelp
    process snapshot on Windows, where `ps` does not exist.

    It returns 0, NOT the immediate parent, when the walk fails. Handing the
    pinger the shell's pid is worse than handing it nothing: the shell is
    already dying, so the pinger would see a dead parent on its first
    iteration and exit having sent one ping -- a session that silently
    produces no heartbeats, which is the bug this code path had once before.
    Worse, a pid that has since been recycled would have the pinger watching
    an unrelated process. 0 means "unwatched": the pinger then relies on the
    stop file and its 12-hour cap, and says so in the log.
    """
    try:
        from rius_cc import platform_compat
        grandparent = platform_compat.parent_pid_of(os.getppid())
    except BaseException:
        return 0
    return grandparent if grandparent > 0 else 0


if __name__ == "__main__":
    main()
    sys.exit(0)
