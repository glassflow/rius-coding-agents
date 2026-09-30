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

from rius_cc import (config, continuation, log as rius_log, otlp,
                     platform_compat, spans, state, subagents, transcript)


# A 5xx or a transport failure may well clear up, so the same lines are
# retried on the next hook event -- but not forever: every retry re-encodes a
# batch that has grown by everything since, so an endpoint that is down for
# an hour has the same unbounded-growth problem as a permanent failure, just
# slower. After this many consecutive failures the batch is dropped.
MAX_CONSECUTIVE_EXPORT_FAILURES = 5

# Events after which nothing else will run for this session. Losing the lock
# here means finalize_session never happens and the root span (plus any open
# turn) stays pending forever, so these wait briefly for it.
FINAL_EVENTS = ("Stop", "SessionEnd")
FINAL_EVENT_LOCK_TIMEOUT_S = 2.0


def _export_error_reason(status: int) -> str:
    """Short, actionable, and free of anything secret."""
    if not status:
        return ("could not reach the endpoint at all (DNS, TLS, network or a "
                "wrong RIUS_ENDPOINT)")
    if status in (401, 403):
        return ("rejected the API key (HTTP %d) -- check RIUS_API_KEY or run "
                "/rius:login; a key minted in the last ~30s is not live yet"
                % status)
    if status == 404:
        return ("no OTLP receiver at that URL (HTTP 404) -- RIUS_ENDPOINT "
                "must be a BASE url; /v1/traces is appended")
    if status == 429:
        return "rate limited (HTTP 429)"
    if 400 <= status < 500:
        return "rejected the payload (HTTP %d)" % status
    return "server error (HTTP %d)" % status


def _is_permanent(status: int) -> bool:
    """4xx means the receiver understood us and said no. Retrying the exact
    same bytes cannot change that -- except for 429, which is a 'later', and
    401: a key minted seconds ago by `/rius:login` is rejected for up to ~30s
    while it reaches the receiver. The transient cap still bounds a key that
    is genuinely wrong."""
    return bool(status) and 400 <= status < 500 and status not in (401, 429)


def _now_rfc3339() -> str:
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _now_ns() -> int:
    try:
        return time.time_ns()
    except AttributeError:  # pragma: no cover - py<3.7
        return int(time.time() * 1e9)


def _log(home: str, cfg, message: str, force: bool = False) -> None:
    rius_log.write(home, cfg, message, force=force)


def _note_skipped_lines(home, cfg, session_id, st, read_stats) -> None:
    """Count unreadable transcript lines into state, and log the first one.

    Dropping lines silently is how a TOTAL parse failure hides: if the
    timestamp format ever shifts, every line is skipped, no span is ever
    built, and the result is indistinguishable from an idle session. The
    counter is what makes it visible; the log says what was wrong.
    """
    skipped = read_stats.get("skipped") or 0
    if not skipped:
        return
    st["lines_skipped"] = st.get("lines_skipped", 0) + skipped
    if st.get("skip_logged"):
        return
    st["skip_logged"] = True
    _log(home, cfg, "session %s: skipped %d unparseable transcript line(s); "
                    "first reason: %s (logged once per session)"
         % (session_id, skipped, read_stats.get("first_skipped_reason")))


def _handle_export_failure(session_id, home, cfg, built_state, new_offset,
                           status) -> int:
    """Record WHY the export failed, and decide whether to keep the lines.

    Two things must be true afterwards. (1) The user can find out: /rius:status
    shows last_export_error, so "Spans exported: 0" is never the whole
    story. (2) The batch cannot grow without bound: a permanent rejection, or
    enough consecutive transient ones, drops these lines by advancing the
    offset. Holding the offset forever against a wrong API key means every
    later hook re-reads from the same place and re-encodes a bigger payload,
    burning CPU on a POST that will never succeed.
    """
    reason = _export_error_reason(status)
    permanent = _is_permanent(status)

    if permanent:
        # Dropping the batch: keep the built state, whose offset now moves
        # past these lines.
        st = built_state
        failures = 0
        st["offset"] = new_offset
        _log(home, cfg, "session %s: PERMANENT export failure, dropping %d "
                        "transcript bytes' worth of spans: %s"
                        % (session_id, new_offset, reason))
    else:
        # Re-read the state as it was BEFORE spans.build() mutated it. The
        # offset is not advancing, so those same lines will be rebuilt next
        # time and the builder's bookkeeping (root_started, open_tools, ...)
        # must not have been persisted in the meantime.
        st = state.load(session_id, home)
        # ...except the "already complained about unreadable lines" flag: the
        # same lines are about to be re-read and re-skipped, and the log is
        # supposed to say so once, not once per retry.
        if built_state.get("skip_logged"):
            st["skip_logged"] = True
        failures = st.get("consecutive_export_failures", 0) + 1
        if failures >= MAX_CONSECUTIVE_EXPORT_FAILURES:
            st = built_state
            st["offset"] = new_offset
            failures = 0
            _log(home, cfg, "session %s: %d consecutive export failures, "
                            "giving up on this batch: %s"
                            % (session_id, MAX_CONSECUTIVE_EXPORT_FAILURES, reason))
        else:
            _log(home, cfg, "session %s: transient export failure %d/%d, will "
                            "retry the same lines: %s"
                            % (session_id, failures,
                               MAX_CONSECUTIVE_EXPORT_FAILURES, reason))

    st["consecutive_export_failures"] = failures
    st["last_export_error"] = {
        "status": status,
        "reason": reason,
        "at": _now_rfc3339(),
    }
    state.save(session_id, home, st)
    return 0


def _ship(out, resource_attrs, st, new_offset, session_id, home, cfg) -> int:
    """Export `out` (if any), then persist the state and the new offset."""
    if out:
        body = otlp.encode(resource_attrs, out)
        status = otlp.export(cfg.endpoint, cfg.api_key, body)
        _log(home, cfg, "session %s: exported %d spans, status=%s"
             % (session_id, len(out), status))

        if not (status and 200 <= status < 300):
            return _handle_export_failure(
                session_id, home, cfg, st, new_offset, status)

        st["spans_exported"] = st.get("spans_exported", 0) + len(out)
        st["consecutive_export_failures"] = 0
        st.pop("last_export_error", None)

    st["offset"] = new_offset
    state.save(session_id, home, st)
    return len(out)


def _run_stopped(event, st, cfg, session_id, cwd, transcript_path, home,
                 end_ns=None) -> int:
    """A session whose folder was disabled after its trace started.

    Disabling beats enabling for content, and for good: the session is
    marked stopped, so re-enabling the folder mid-session does not resume it
    (its transcript offset has not moved, so resuming would upload what was
    said while it was off). Nothing is read from the transcript. SessionEnd
    still closes every open span -- with capture off, so the closing spans
    carry no content -- because a single pending span keeps the whole
    session out of the backend's finished-trace counts.
    """
    if not state.trace_is_open(st):
        _log(home, cfg, "session %s: %s" % (session_id, cfg.reason))
        return 0
    st["content_stopped"] = True
    if event != "SessionEnd":
        state.save(session_id, home, st)
        _log(home, cfg, "session %s: stopped (%s); not exporting %s"
             % (session_id, cfg.reason, event))
        return 0

    ctx = spans.Ctx(
        session_id=session_id, cwd=cwd, git_branch="", cc_version="",
        service_name=cfg.service_name, capture_content=False,
        max_attr_bytes=cfg.max_attr_bytes,
    )
    now_ns = end_ns or _now_ns()
    out = []
    try:
        out += subagents.finalize(
            st, ctx, subagents.dir_for(transcript_path, session_id), now_ns)
    except Exception as exc:
        _log(home, cfg, "session %s: subagent finalize failed: %r"
             % (session_id, exc), force=True)
    out += spans.finalize_session(st, ctx, now_ns)
    resource_attrs = {
        "service.name": cfg.service_name,
        "service.instance.id": st.get("instance_id") or "",
        "cc.version": "",
        "cc.cwd": cwd,
        "cc.git_branch": "",
    }
    return _ship(out, resource_attrs, st, st.get("offset", 0), session_id,
                 home, cfg)


def _stop_heartbeat(session_id: str, home: str) -> None:
    """What hook.py does on SessionEnd: tell that session's pinger to stop."""
    try:
        with open(os.path.join(state.state_dir(home),
                               session_id + ".heartbeat.stop"), "w") as fh:
            fh.write("")
    except OSError:
        pass


def _close_predecessor(old_id, old_path, switch_ns, cwd, env, home) -> None:
    """End the trace of the session this one continues, at the switch.

    Its process never gets a SessionEnd, so without this its trace stays
    open for good. It is closed exactly as its own SessionEnd would have
    closed it -- including #7's rules for a session that was stopped -- and
    only if it is still open.
    """
    if not state.trace_is_open(state.load(old_id, home)):
        return
    run("SessionEnd", {"session_id": old_id, "cwd": cwd,
                       "transcript_path": old_path}, env, home,
        end_ns=switch_ns or None)
    _stop_heartbeat(old_id, home)


def _skip_continued_history(st, entries, session_id, transcript_path, cwd,
                            env, home, cfg):
    """Drop the history Claude Code copied from the session this continues.

    Checked once, on the first read that has entries: the copy is written
    when the new session starts, before its first hook. The old
    transcript's uuids are read only while the copied prefix lasts.
    """
    if entries and not st.get("continuation_checked"):
        st["continuation_checked"] = True
        found = continuation.find_predecessor(transcript_path, session_id)
        if found:
            old_id, old_path, switch_ns = found
            st["continued_from"] = old_id
            st["continued_from_path"] = old_path
            st["skipping_copied"] = True
            _log(home, cfg, "session %s: continues session %s; skipping the "
                            "history it already sent" % (session_id, old_id))
            try:
                _close_predecessor(old_id, old_path, switch_ns, cwd, env, home)
            except Exception as exc:
                _log(home, cfg, "session %s: closing session %s failed: %r"
                     % (session_id, old_id, exc), force=True)
    if st.get("skipping_copied") and entries:
        uuids = continuation.copied_uuids(st.get("continued_from_path") or "")
        entries, ended = continuation.drop_copied(entries, uuids)
        if ended:
            st["skipping_copied"] = False
    return entries


def run(event: str, payload: dict, env: Mapping[str, str], home: str,
        instance_id: str = "", end_ns=None) -> int:
    """`end_ns` closes the session at that time instead of now (SessionEnd)."""
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
        if not cfg.enabled and not cfg.api_key:
            _log(home, cfg, "session %s: %s" % (session_id, cfg.reason))
            return 0

        block_timeout = (FINAL_EVENT_LOCK_TIMEOUT_S
                         if event in FINAL_EVENTS else 0.0)
        with state.session_lock(session_id, home,
                                block_timeout=block_timeout) as acquired:
            if not acquired:
                _log(home, cfg, "session %s: lock held, skipping %s"
                     % (session_id, event))
                return 0

            st = state.load(session_id, home)
            if not cfg.enabled or st.get("content_stopped"):
                return _run_stopped(event, st, cfg, session_id, cwd,
                                    transcript_path, home, end_ns=end_ns)

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

            read_stats = {}
            entries, new_offset = transcript.read_from(
                transcript_path, st.get("offset", 0), stats=read_stats)
            _note_skipped_lines(home, cfg, session_id, st, read_stats)
            entries = _skip_continued_history(st, entries, session_id,
                                              transcript_path, cwd, env, home,
                                              cfg)

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

            out = spans.build(entries, st, ctx, source_path=transcript_path)

            # Subagents write their own transcripts; without this the 58% of
            # tokens that live in them never reach the trace. Guarded on its
            # own: a surprise in those files must not cost the main
            # transcript's spans, which are already built by this point.
            sub_dir = subagents.dir_for(transcript_path, session_id)
            try:
                out += subagents.expand(st, ctx, sub_dir)
            except Exception as exc:
                _log(home, cfg, "session %s: subagent expansion failed: %r"
                     % (session_id, exc), force=True)

            now_ns = end_ns or _now_ns()
            if event == "Stop":
                out += spans.finalize_turn(st, ctx, now_ns)
            elif event == "SessionEnd":
                try:
                    out += subagents.finalize(st, ctx, sub_dir, now_ns)
                except Exception as exc:
                    _log(home, cfg, "session %s: subagent finalize failed: %r"
                         % (session_id, exc), force=True)
                out += spans.finalize_session(st, ctx, now_ns)

            resource_attrs = {
                "service.name": cfg.service_name,
                "service.instance.id": instance_id,
                "cc.version": first_cc_version,
                "cc.cwd": first_cwd,
                "cc.git_branch": first_git_branch,
            }
            return _ship(out, resource_attrs, st, new_offset, session_id,
                         home, cfg)
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
        home = platform_compat.home_dir(os.environ)
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
