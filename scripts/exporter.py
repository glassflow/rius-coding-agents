"""Wires transcript reading, span building, OTLP encoding and export into one
process invoked once per Claude Code hook event.

Zero third-party dependencies -- only the standard library and rius_cc.
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid
from typing import Mapping

from rius_cc import config, log as rius_log, otlp, spans, state, transcript


def _now_ns() -> int:
    try:
        return time.time_ns()
    except AttributeError:  # pragma: no cover - py<3.7
        return int(time.time() * 1e9)


def _log(home: str, cfg, message: str, force: bool = False) -> None:
    rius_log.write(home, cfg, message, force=force)


def run(event: str, payload: dict, env: Mapping[str, str], home: str,
        instance_id: str = "") -> int:
    cfg = None
    try:
        session_id = payload.get("session_id")
        cwd = payload.get("cwd")
        transcript_path = payload.get("transcript_path")

        if not session_id or not cwd or not transcript_path:
            return 0

        cfg = config.resolve(session_id, cwd, env, home)
        _log(home, cfg, "session %s: resolved config, api_key=%s, endpoint=%s"
             % (session_id, config.redact(cfg.api_key), cfg.endpoint))
        if not cfg.enabled:
            _log(home, cfg, "session %s: %s" % (session_id, cfg.reason))
            return 0

        with state.session_lock(session_id, home) as acquired:
            if not acquired:
                _log(home, cfg, "session %s: lock held, skipping" % session_id)
                return 0

            st = state.load(session_id, home)

            # Mint and persist the instance id unconditionally, before any
            # export decision. A heartbeat pinger starts at SessionStart and
            # must be able to send this id on its very first ping, even for
            # a session that starts and then sits idle with nothing to
            # export -- so this cannot wait on there being spans to send.
            # hook.py mints it on SessionStart and passes it on argv, so
            # normally this only re-reads what is already persisted.
            stored = st.get("instance_id")
            if not stored:
                stored = instance_id or str(uuid.uuid4())
                st["instance_id"] = stored
                state.save(session_id, home, st)
            instance_id = stored

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
                    # Export failed: do NOT advance the offset and do NOT
                    # count these spans as exported, so the next hook
                    # invocation retries these same transcript lines.
                    return 0

                st["spans_exported"] = st.get("spans_exported", 0) + len(out)

            st["offset"] = new_offset
            state.save(session_id, home, st)

            return len(out)
    except BaseException as exc:  # never raise out of run()
        try:
            # force=True deliberately: an unhandled crash is logged even with
            # RIUS_CLAUDE_DEBUG off. A crash you cannot see is the exact
            # failure mode this plugin has to avoid. Documented in
            # README > Settings. `cfg` is passed when it exists purely so the
            # API key is still redacted out of the message.
            _log(home, cfg, "exception in run(): %r" % (exc,), force=True)
        except BaseException:
            pass
        return 0


def main() -> None:
    try:
        payload_path = sys.argv[1]
        instance_id = sys.argv[2] if len(sys.argv) > 2 else ""
        with open(payload_path) as fh:
            wrapper = json.load(fh)
        event = wrapper.get("event")
        payload = wrapper.get("payload") or {}
        home = os.environ.get("HOME", os.path.expanduser("~"))
        run(event, payload, os.environ, home, instance_id)
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
