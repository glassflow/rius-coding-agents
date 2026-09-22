"""Wires transcript reading, span building, OTLP encoding and export into one
process invoked once per Claude Code hook event.

Zero third-party dependencies -- only the standard library and rius_cc.
"""
from __future__ import annotations

import datetime
import json
import os
import sys
import time
import uuid
from typing import Mapping

from rius_cc import config, otlp, spans, state, transcript


def _now_ns() -> int:
    try:
        return time.time_ns()
    except AttributeError:  # pragma: no cover - py<3.7
        return int(time.time() * 1e9)


def _log(home: str, cfg, message: str) -> None:
    if not getattr(cfg, "debug", False):
        return
    try:
        text = message
        api_key = getattr(cfg, "api_key", None)
        if api_key:
            text = text.replace(api_key, config.redact(api_key))
        log_dir = os.path.join(home, ".claude", "rius", "log")
        os.makedirs(log_dir, exist_ok=True)
        date = datetime.datetime.utcnow().strftime("%Y-%m-%d")
        path = os.path.join(log_dir, date + ".log")
        with open(path, "a") as fh:
            fh.write(text.rstrip("\n") + "\n")
    except BaseException:
        pass


def run(event: str, payload: dict, env: Mapping[str, str], home: str) -> int:
    try:
        session_id = payload.get("session_id")
        cwd = payload.get("cwd")
        transcript_path = payload.get("transcript_path")

        if not session_id or not cwd or not transcript_path:
            return 0

        cfg = config.resolve(session_id, cwd, env, home)
        if not cfg.enabled:
            _log(home, cfg, "session %s: %s" % (session_id, cfg.reason))
            return 0

        with state.session_lock(session_id, home) as acquired:
            if not acquired:
                _log(home, cfg, "session %s: lock held, skipping" % session_id)
                return 0

            st = state.load(session_id, home)

            entries, new_offset = transcript.read_from(transcript_path, st.get("offset", 0))

            first_cwd = cwd
            first_git_branch = ""
            first_cc_version = ""
            if entries:
                first_cwd = entries[0].cwd or cwd
                first_git_branch = entries[0].git_branch or ""
                first_cc_version = entries[0].cc_version or ""

            ctx = spans.Ctx(
                session_id=session_id,
                cwd=first_cwd,
                git_branch=first_git_branch,
                cc_version=first_cc_version,
                service_name=cfg.service_name,
                capture_content=cfg.capture_content,
                max_attr_bytes=cfg.max_attr_bytes,
            )

            out = spans.build(entries, st, ctx)

            now_ns = _now_ns()
            if event == "Stop":
                out += spans.finalize_turn(st, ctx, now_ns)
            elif event == "SessionEnd":
                out += spans.finalize_session(st, ctx, now_ns)

            if out:
                instance_id = st.get("instance_id")
                if not instance_id:
                    instance_id = str(uuid.uuid4())
                    st["instance_id"] = instance_id

                resource_attrs = {
                    "service.name": cfg.service_name,
                    "service.instance.id": instance_id,
                    "cc.version": first_cc_version,
                    "cc.cwd": first_cwd,
                    "cc.git_branch": first_git_branch,
                }
                body = otlp.encode(resource_attrs, out)
                status = otlp.export(cfg.endpoint, cfg.api_key, body)
                _log(home, cfg, "session %s: exported %d spans, status=%s"
                     % (session_id, len(out), status))

                if not (status and 200 <= status < 300):
                    # Export failed: do NOT advance the offset, so the next
                    # hook invocation retries these same transcript lines.
                    return 0

            st["offset"] = new_offset
            state.save(session_id, home, st)

            return len(out)
    except BaseException as exc:  # never raise out of run()
        try:
            _log(home, config.Config(False, "exception", None, "", "", True,
                                      config.DEFAULT_MAX_ATTR_BYTES, True),
                 "exception in run(): %r" % (exc,))
        except BaseException:
            pass
        return 0


def main() -> None:
    try:
        payload_path = sys.argv[1]
        with open(payload_path) as fh:
            wrapper = json.load(fh)
        event = wrapper.get("event")
        payload = wrapper.get("payload") or {}
        home = os.environ.get("HOME", os.path.expanduser("~"))
        run(event, payload, os.environ, home)
        try:
            os.remove(payload_path)
        except OSError:
            pass
    except BaseException:
        pass
    finally:
        sys.exit(0)


if __name__ == "__main__":
    main()
