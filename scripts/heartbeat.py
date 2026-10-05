#!/usr/bin/env python3
"""Agent-lifetime heartbeat pinger for a single Claude Code session.

Spawned detached by hook.py on SessionStart (mirroring how the exporter is
spawned), this process pings ``POST <endpoint>/v1/heartbeat`` with a small
JSON payload (v1) every ``INTERVAL_S`` seconds for as long as the session it
watches is alive, then sends one final ``stopped: true`` ping and exits.

Traces only answer "did something happen" -- they export when a span
finishes. This answers "is the agent alive right now", independent of trace
traffic, which is exactly what an idle-but-healthy session cannot otherwise
signal.

Contract (payload v1), verified against the Rius Python SDK
(``glassflow-python/src/rius/heartbeat.py``) and its config
(``glassflow-python/src/rius/config.py``):

- ``POST <endpoint>/v1/heartbeat`` -- JSON, unlike the protobuf-only
  ``/v1/traces``. URL is built as ``endpoint.rstrip("/") + "/v1/heartbeat"``;
  the endpoint is a bare base URL with no path.
- Keys exactly: v, instance_id, agent_name, sent_at, sdk_language,
  sdk_version, open_traces, open_trace_count. ``sent_at`` is RFC3339 UTC with
  millisecond precision and a ``Z`` suffix. ``open_traces`` is capped at 32
  entries; ``open_trace_count`` is the true, uncapped count. This process has
  no visibility into open root spans (that lives in the exporter's per-hook
  process, which does not persist across pings), so both are always empty/0
  here -- liveness, not span state, is this process's job.
- ``stopped: true`` appears ONLY on the final ping. ``false`` is never sent.
- Interval 15s. Ping timeout 3s; final stopped ping 1s. Pings are NEVER
  retried: liveness is only true fresh, so a failed ping is logged once (via
  a best-effort debug log) and dropped, not queued or retried.

Lifetime -- exits on the FIRST of:
  1. The stop file appears (written by hook.py on SessionEnd). Sends the
     final ``stopped: true`` ping first.
  2. The watched Claude Code process is no longer alive. Exits WITHOUT a
     stopped ping -- claiming a clean stop for a killed process would be a
     lie; the backend's stale -> gone path exists for exactly this case.
     The liveness probe lives in ``rius_cc.platform_compat.pid_alive``:
     ``os.kill(pid, 0)`` is a harmless probe on POSIX but an ATTACK on
     Windows (see that module), so it is never called there. A watch pid of
     0 means "the parent could not be identified"; the pinger then runs
     unwatched, bounded by the stop file and the 12-hour cap, rather than
     exiting at once and producing no heartbeats at all.
  3. A 12-hour absolute cap, so no bug can leave a pinger running forever.
     Exits WITHOUT a stopped ping, exactly like the dead-parent case:
     ``stopped`` describes the AGENT, not the pinger, and a session still
     running at hour 12 is plausible for this product's target long-lived
     agents. The cap bounds the pinger's own lifetime; it is not evidence
     the agent stopped. The backend's stale -> gone path covers this.

A pid file prevents duplicate pingers for the same session: if one exists
and names a live process, this process exits immediately.

Zero third-party dependencies -- standard library only. Must run under
Python 3.9.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rius_cc import platform_compat  # noqa: E402

PAYLOAD_VERSION = 1
OPEN_TRACES_CAP = 32
SDK_LANGUAGE = "python"
SDK_VERSION = "0.1.0"

PING_TIMEOUT_S = 3.0
FINAL_PING_TIMEOUT_S = 1.0
INTERVAL_S = 15.0
POLL_INTERVAL_S = 0.5
MAX_LIFETIME_S = 12 * 60 * 60


def heartbeat_url(endpoint: str) -> str:
    """<endpoint>/v1/heartbeat, exactly one slash regardless of trailing /."""
    return endpoint.rstrip("/") + "/v1/heartbeat"


def now_rfc3339_millis() -> str:
    """RFC3339 UTC, millisecond precision, Z suffix."""
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + ("%03dZ" % (now.microsecond // 1000))


def build_payload(instance_id: str, agent_name: str, stopped: bool = False) -> dict:
    payload = {
        "v": PAYLOAD_VERSION,
        "instance_id": instance_id,
        "agent_name": agent_name,
        "sent_at": now_rfc3339_millis(),
        "sdk_language": SDK_LANGUAGE,
        "sdk_version": SDK_VERSION,
        "open_traces": [],
        "open_trace_count": 0,
    }
    if stopped:
        # Present-and-true only on the final ping; false is never sent.
        payload["stopped"] = True
    return payload


def http_transport(url: str, api_key: str):
    def send(payload: dict, timeout: float) -> None:
        headers = {
            "Content-Type": "application/json",
            "Authorization": "Bearer " + api_key,
        }
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=timeout):
            pass

    return send


class Pinger:
    """Polls at a fine grain but pings at ``interval``; never retries a ping."""

    def __init__(self, session_id, home, instance_id, agent_name, transport,
                 watch_pid, interval=INTERVAL_S, max_lifetime=MAX_LIFETIME_S,
                 ping_timeout=PING_TIMEOUT_S, final_ping_timeout=FINAL_PING_TIMEOUT_S,
                 poll_interval=POLL_INTERVAL_S, clock=time.monotonic, sleep=time.sleep,
                 log=None):
        self.session_id = session_id
        self.home = home
        self.instance_id = instance_id
        self.agent_name = agent_name
        self.transport = transport
        self.watch_pid = watch_pid
        self.interval = interval
        self.max_lifetime = max_lifetime
        self.ping_timeout = ping_timeout
        self.final_ping_timeout = final_ping_timeout
        self.poll_interval = poll_interval
        self.clock = clock
        self.sleep = sleep
        self._log = log or (lambda _msg: None)

    def _state_dir(self) -> str:
        d = os.path.join(self.home, ".claude", "rius", "state")
        os.makedirs(d, exist_ok=True)
        return d

    def stop_path(self) -> str:
        return os.path.join(self._state_dir(), self.session_id + ".heartbeat.stop")

    def pid_path(self) -> str:
        return os.path.join(self._state_dir(), self.session_id + ".heartbeat.pid")

    def _pid_alive(self, pid: int) -> bool:
        """Never os.kill: on Windows that terminates or Ctrl+C's the target."""
        return platform_compat.pid_alive(pid)

    def acquire_pid_lock(self) -> bool:
        """False if another live pinger already owns this session."""
        path = self.pid_path()
        try:
            with open(path) as fh:
                existing = int(fh.read().strip())
            if self._pid_alive(existing):
                return False
        except (OSError, ValueError):
            pass  # missing or stale/corrupt -- take over
        try:
            with open(path, "w") as fh:
                fh.write(str(os.getpid()))
        except OSError:
            pass
        return True

    def clear_stop_file(self) -> None:
        """Remove a stop file left over from a previous run of this session.

        Only ever called AFTER the pid lock is won, and only at startup: at
        that instant this session cannot yet have ended, so any stop file
        present is stale (hook.py wrote it on a previous SessionEnd and
        nothing else deletes it). Clearing it after winning the lock also
        means a duplicate pinger cannot swallow the signal meant for the
        live one.
        """
        try:
            os.remove(self.stop_path())
            self._log("cleared a stale stop file at startup")
        except OSError:
            pass

    def release_pid_lock(self) -> None:
        try:
            os.remove(self.pid_path())
        except OSError:
            pass

    def _send(self, stopped: bool) -> None:
        timeout = self.final_ping_timeout if stopped else self.ping_timeout
        payload = build_payload(self.instance_id, self.agent_name, stopped=stopped)
        try:
            self.transport(payload, timeout)
        except Exception as exc:  # never retried, never raised
            self._log("heartbeat delivery failed: %r" % (exc,))

    def run(self) -> None:
        if not self.acquire_pid_lock():
            self._log("another live pinger already owns this session")
            return
        try:
            if self.watch_pid <= 0:
                # Said out loud rather than swallowed: without a watch pid
                # the only exits left are the stop file and the cap.
                self._log("no watchable parent pid; running unwatched until "
                          "the stop file or the lifetime cap")
            self.clear_stop_file()
            start = self.clock()
            self._send(stopped=False)
            last_ping = self.clock()
            while True:
                if os.path.exists(self.stop_path()):
                    self._send(stopped=True)
                    return
                if self.watch_pid > 0 and not self._pid_alive(self.watch_pid):
                    return  # killed process: no stopped ping, that would lie
                if self.clock() - start >= self.max_lifetime:
                    return  # cap on the PINGER, not evidence the agent
                            # stopped: a live agent past 12h must not be
                            # reported stopped; let stale->gone handle it
                if self.clock() - last_ping >= self.interval:
                    self._send(stopped=False)
                    last_ping = self.clock()
                self.sleep(self.poll_interval)
        finally:
            self.release_pid_lock()


def main() -> None:
    if len(sys.argv) < 5:
        return
    session_id, cwd, home, watch_pid_s = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
    # hook.py mints the instance id before spawning anything and passes it
    # here; state is only a fallback (a hook from an older install, say).
    argv_instance_id = sys.argv[5] if len(sys.argv) > 5 else ""
    try:
        watch_pid = int(watch_pid_s)
    except ValueError:
        return

    from rius_cc import config, log as rius_log, state

    cfg = config.resolve(session_id, cwd, os.environ, home)

    def log(message: str) -> None:
        rius_log.write(home, cfg, "heartbeat %s: %s" % (session_id, message))

    if not cfg.enabled or not cfg.api_key:
        log("not starting: %s" % cfg.reason)
        return

    instance_id = argv_instance_id or state.load(session_id, home).get("instance_id")
    if not instance_id:
        # Never invent one -- it must match spans' service.instance.id.
        log("not starting: no instance_id on argv and none in state")
        return

    url = heartbeat_url(cfg.endpoint)
    transport = http_transport(url, cfg.api_key)
    log("starting: watching pid %s, posting to %s"
        % (watch_pid if watch_pid > 0 else "<unknown>", url))
    pinger = Pinger(session_id, home, instance_id, cfg.service_name, transport,
                    watch_pid, log=log)
    pinger.run()


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        pass
