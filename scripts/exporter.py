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

from rius_cc import (agent, config, continuation, log as rius_log, otlp,
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

# A session whose trace is still open this long after it was last seen, and
# whose Claude Code process and pinger are both gone, is taken to be dead.
# The pinger's own cap (heartbeat.MAX_LIFETIME_S): it covers a session whose
# process could not be identified.
STALE_AFTER_S = 12 * 60 * 60


def _export_error_reason(status: int) -> str:
    """Short, actionable, and free of anything secret."""
    if not status:
        return ("could not reach the endpoint at all (DNS, TLS, network, or a "
                "wrong or non-https RIUS_ENDPOINT)")
    if 300 <= status < 400:
        return ("redirected (HTTP %d); redirects are not followed, so the key "
                "is never sent on -- set RIUS_ENDPOINT to the final URL" % status)
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


def _present(attrs: dict) -> dict:
    """An unknown resource value (no git repo, no CC version yet) is left out,
    so it stays absent rather than arriving as an empty string."""
    return {k: v for k, v in attrs.items() if v is not None and v != ""}


def _ship(out, resource_attrs, st, new_offset, session_id, home, cfg,
          on_success=None) -> int:
    """Export `out` (if any), then persist the state and the new offset.

    `on_success` runs only once the spans are accepted (or there were none).
    """
    if out:
        body = otlp.encode(_present(resource_attrs), out)
        status = otlp.export(cfg.endpoint, cfg.api_key, body)
        _log(home, cfg, "session %s: exported %d spans, status=%s"
             % (session_id, len(out), status))

        if not (status and 200 <= status < 300):
            return _handle_export_failure(
                session_id, home, cfg, st, new_offset, status)

        if st.get("root_started") and not st.get("key_fingerprint"):
            # The key the trace was opened with, as a hash: only a session
            # using that same key may close it (sweep_stale). Set once the
            # receiver has accepted spans under it, and never moved after.
            st["key_fingerprint"] = config.key_fingerprint(
                cfg.api_key, cfg.endpoint)
        st["spans_exported"] = st.get("spans_exported", 0) + len(out)
        st["consecutive_export_failures"] = 0
        st.pop("last_export_error", None)

    st["offset"] = new_offset
    state.save(session_id, home, st)
    if on_success is not None:
        on_success()
    return len(out)


def _ctx(st, session_id, cfg, cwd, git_branch="", cc_version="",
         capture_content=None):
    """The span context for this session id: under its conversation's trace."""
    return spans.Ctx(
        session_id=session_id, cwd=cwd, git_branch=git_branch,
        cc_version=cc_version, service_name=cfg.service_name,
        capture_content=(cfg.capture_content if capture_content is None
                         else capture_content),
        max_attr_bytes=cfg.max_attr_bytes,
        conversation_id=st.get("conversation") or "",
        continued_from=st.get("continued_from") or "",
    )


def _adopted_subagents(st, ctx, cfg, home, event, now_ns, final):
    """Spans from the subagents of the session ids this one took over.

    A background subagent can outlive the switch: its file sits under the
    OLD id's directory, and its bookkeeping stays in the old id's state.
    Returns (spans, states to save once the export succeeded).
    """
    out, saves = [], []
    for item in st.get("adopted") or []:
        old_id = item.get("session_id") if isinstance(item, dict) else None
        if not old_id:
            continue
        with state.session_lock(old_id, home, block_timeout=(
                FINAL_EVENT_LOCK_TIMEOUT_S if event in FINAL_EVENTS else 0.0)) as got:
            if not got:
                continue
            old = state.load(old_id, home)
        if not old.get("sub_links"):
            continue
        old_ctx = spans.Ctx(
            session_id=old_id, cwd=ctx.cwd, git_branch=ctx.git_branch,
            cc_version=ctx.cc_version, service_name=ctx.service_name,
            capture_content=ctx.capture_content,
            max_attr_bytes=ctx.max_attr_bytes,
            conversation_id=ctx.conversation_id)
        sub_dir = subagents.dir_for(item.get("transcript_path") or "", old_id)
        try:
            if not final:
                out += subagents.expand(old, old_ctx, sub_dir)
            if event == "SessionEnd":
                out += subagents.finalize(old, old_ctx, sub_dir, now_ns)
        except Exception as exc:
            _log(home, cfg, "session %s: subagents of %s failed: %r"
                 % (ctx.session_id, old_id, exc), force=True)
        saves.append((old_id, old))
    return out, saves


def _save_adopted(saves, home):
    for old_id, old in saves:
        with state.session_lock(old_id, home,
                                block_timeout=FINAL_EVENT_LOCK_TIMEOUT_S) as got:
            if got:
                state.save(old_id, home, old)


def _on_shipped(session_id, home, st, saves):
    """What follows an accepted export: the adopted subagents' states, and
    the open-trace marker the next SessionStart's sweep looks for."""
    def done():
        _save_adopted(saves, home)
        state.sync_open_marker(session_id, home, st)
    return done


def _resource_attrs(cfg, instance_id, cwd, version="", git_branch=""):
    prefix = agent.active().attr_prefix
    return {
        "service.name": cfg.service_name,
        "service.instance.id": instance_id,
        prefix + "version": version,
        prefix + "cwd": cwd,
        prefix + "git_branch": git_branch,
    }


def _close_trace(st, cfg, session_id, cwd, transcript_path, home,
                 end_ns) -> int:
    """Close every open span of the session, with no content, reading
    nothing from its transcript."""
    ctx = _ctx(st, session_id, cfg, cwd, capture_content=False)
    out = []
    try:
        out += subagents.finalize(
            st, ctx, subagents.dir_for(transcript_path, session_id), end_ns)
    except Exception as exc:
        _log(home, cfg, "session %s: subagent finalize failed: %r"
             % (session_id, exc), force=True)
    adopted, saves = _adopted_subagents(st, ctx, cfg, home, "SessionEnd",
                                        end_ns, final=True)
    out += adopted
    out += spans.finalize_session(st, ctx, end_ns)
    resource_attrs = _resource_attrs(cfg, st.get("instance_id") or "", cwd)
    return _ship(out, resource_attrs, st, st.get("offset", 0), session_id,
                 home, cfg, on_success=_on_shipped(session_id, home, st, saves))


def _last_seen_ns(st) -> int:
    """When the session was last seen doing anything, in any transcript."""
    scopes = [st] + list((st.get("sub_scopes") or {}).values())
    return max([st.get("root_start_ns") or 0]
               + [scope.get("last_ns") or 0 for scope in scopes])


def _close_if_stale(cfg, session_id, home, fingerprint, now_ns) -> None:
    with state.session_lock(session_id, home) as got:
        if not got:
            return
        st = state.load(session_id, home)
        if not state.trace_is_open(st) or st.get("handed_off_to"):
            state.sync_open_marker(session_id, home, st)
            return
        # Sent with any other key, the closing spans would land in that
        # key's workspace, or none: a project's own RIUS_API_KEY can point
        # this session and that one at different tenants.
        if st.get("key_fingerprint") != fingerprint:
            return
        last_seen_ns = _last_seen_ns(st)
        if (now_ns - last_seen_ns < STALE_AFTER_S * 10**9
                or platform_compat.pid_alive(st.get("cc_pid") or 0)
                or state.pinger_alive(session_id, home)):
            # Idle, not dead: a session can sit at a prompt for days.
            return
        _log(home, cfg, "session %s: never ended; closing its trace"
             % session_id)
        # Ended when it was last seen, not now: a trace that draws as days
        # long would be its own bug.
        _close_trace(st, cfg, session_id, st.get("root_cwd") or "",
                     st.get("transcript_path") or "", home, last_seen_ns)


def sweep_stale(cfg, current_id, home, now_ns=None) -> None:
    """Close the traces of sessions that died without a SessionEnd.

    Run by the exporter on SessionStart, never by the hook: it is off the
    critical path. Only traces last sent with this same key are touched.
    """
    fingerprint = config.key_fingerprint(cfg.api_key, cfg.endpoint)
    if not fingerprint:
        return
    now_ns = _now_ns() if now_ns is None else now_ns
    for session_id in state.open_marked_sessions(home):
        if session_id == current_id:
            continue
        try:
            _close_if_stale(cfg, session_id, home, fingerprint, now_ns)
        except Exception as exc:
            _log(home, cfg, "session %s: stale-trace sweep failed: %r"
                 % (session_id, exc), force=True)


def _run_stopped(event, st, cfg, session_id, cwd, transcript_path, home) -> int:
    """A session whose folder was disabled after its trace started.

    Disabling beats enabling for content, and for good: the session is
    marked stopped, so re-enabling the folder mid-session does not resume it
    (its transcript offset has not moved, so resuming would upload what was
    said while it was off). Nothing is read from the transcript. SessionEnd
    still closes every open span -- with capture off, so the closing spans
    carry no content -- because a single pending span keeps the whole
    session out of the backend's finished-trace counts. A conversation that
    moved to this id brings its stop with it, and is closed the same way.
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
    return _close_trace(st, cfg, session_id, cwd, transcript_path, home,
                        _now_ns())


def _link(st, session_id, transcript_path, home, cfg, final):
    """Take over the conversation this id continues, if it continues one."""
    try:
        old_id = continuation.link(session_id, transcript_path, home, st=st,
                                   final=final)
    except Exception as exc:
        _log(home, cfg, "session %s: continuation check failed: %r"
             % (session_id, exc), force=True)
        return
    if old_id:
        # Persisted at once: a failed export reloads this state from disk,
        # and the old id has already handed its root over.
        state.save(session_id, home, st)
        _log(home, cfg, "session %s: continues the conversation of %s in "
                        "its trace" % (session_id, old_id))


def _drop_copied_history(st, entries):
    """The history Claude Code copied in, minus what was never sent."""
    if not st.get("skipping_copied") or not entries:
        return entries
    uuids = continuation.copied_uuids(st.get("continued_from_path") or "",
                                      until=st.get("copied_until", -1))
    entries, ended = continuation.drop_copied(entries, uuids)
    if ended:
        st["skipping_copied"] = False
    return entries


def _run_session(event, cfg, session_id, cwd, transcript_path, home,
                 instance_id) -> int:
    block_timeout = (FINAL_EVENT_LOCK_TIMEOUT_S
                     if event in FINAL_EVENTS else 0.0)
    with state.session_lock(session_id, home,
                            block_timeout=block_timeout) as acquired:
        if not acquired:
            _log(home, cfg, "session %s: lock held, skipping %s"
                 % (session_id, event))
            return 0

        st = state.load(session_id, home)
        if st.get("handed_off_to"):
            # Claude Code moved this conversation to another session id,
            # which owns its trace and root from the switch on.
            _log(home, cfg, "session %s: handed off to %s; %s ignored"
                 % (session_id, st["handed_off_to"], event))
            return 0
        # Before the stopped check: a conversation moved into a disabled
        # folder must still be found, or its root is never closed.
        _link(st, session_id, transcript_path, home, cfg, final=False)
        if not cfg.enabled or st.get("content_stopped"):
            return _run_stopped(event, st, cfg, session_id, cwd,
                                transcript_path, home)

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
        titles = {}
        entries, new_offset = transcript.read_from(
            transcript_path, st.get("offset", 0), stats=read_stats,
            titles=titles)
        _note_skipped_lines(home, cfg, session_id, st, read_stats)
        if entries and not st.get("continuation_checked"):
            _link(st, session_id, transcript_path, home, cfg, final=True)
            if st.get("content_stopped"):
                return _run_stopped(event, st, cfg, session_id, cwd,
                                    transcript_path, home)
        entries = _drop_copied_history(st, entries)

        first_cwd = cwd
        first_git_branch = ""
        first_cc_version = ""
        if entries:
            first_cwd = entries[0].cwd or cwd
            first_git_branch = entries[0].git_branch or ""
            first_cc_version = entries[0].cc_version or ""

        ctx = _ctx(st, session_id, cfg, first_cwd, first_git_branch,
                   first_cc_version)

        out = spans.build(entries, st, ctx, source_path=transcript_path,
                          titles=titles)
        if st.get("root_started"):
            # What the sweep needs to close this trace if the session
            # dies without a SessionEnd.
            st.setdefault("root_cwd", first_cwd)
            st["transcript_path"] = transcript_path

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

        now_ns = _now_ns()
        adopted, saves = _adopted_subagents(st, ctx, cfg, home, event,
                                            now_ns, final=False)
        out += adopted
        if event == "Stop":
            out += spans.finalize_turn(st, ctx, now_ns)
        elif event == "SessionEnd":
            try:
                out += subagents.finalize(st, ctx, sub_dir, now_ns)
            except Exception as exc:
                _log(home, cfg, "session %s: subagent finalize failed: %r"
                     % (session_id, exc), force=True)
            out += spans.finalize_session(st, ctx, now_ns)

        resource_attrs = _resource_attrs(cfg, instance_id, first_cwd,
                                         first_cc_version, first_git_branch)
        return _ship(out, resource_attrs, st, new_offset, session_id,
                     home, cfg,
                     on_success=_on_shipped(session_id, home, st, saves))


def run(event: str, payload: dict, env: Mapping[str, str], home: str,
        instance_id: str = "") -> int:
    cfg = None
    try:
        session_id = payload.get("session_id")
        cwd = payload.get("cwd")
        transcript_path = payload.get("transcript_path")

        if not cwd or not transcript_path:
            return 0
        if not state.is_valid_session_id(session_id):
            return 0

        cfg = config.resolve(session_id, cwd, env, home)
        _log(home, cfg, "session %s: resolved config, api_key=%s, endpoint=%s"
             % (session_id, config.redact(cfg.api_key), cfg.endpoint))
        if not cfg.enabled and not cfg.api_key:
            _log(home, cfg, "session %s: %s" % (session_id, cfg.reason))
            return 0

        result = _run_session(event, cfg, session_id, cwd, transcript_path,
                              home, instance_id)
        if event == "SessionStart" and cfg.enabled and cfg.api_key:
            # After this session's own export: by then it has taken over any
            # conversation it continues, so that one's root is not swept.
            sweep_stale(cfg, session_id, home)
        return result
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
        profile, argv = agent.from_argv(sys.argv[1:], os.environ)
        agent.activate(profile)
        payload_path = argv[0]
        instance_id = argv[1] if len(argv) > 1 else ""
        try:
            with open(payload_path) as fh:
                wrapper = json.load(fh)
        finally:
            # Prompt text and tool output: gone as soon as it is read, even
            # when it does not parse.
            try:
                os.remove(payload_path)
            except OSError:
                pass
        event = wrapper.get("event")
        payload = wrapper.get("payload") or {}
        home = platform_compat.home_dir(os.environ)
        run(event, payload, os.environ, home, instance_id)
    except BaseException:
        pass
    finally:
        sys.exit(0)


if __name__ == "__main__":
    main()
