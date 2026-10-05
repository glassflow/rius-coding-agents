"""Systemic guard: with capture off, no Cursor content reaches the spool.

The spool is plaintext on disk for as long as the conversation lives, the
same hazard test_content_never_in_state.py guards for Claude Code's state
file. Driven off the fixtures: every string under a content field of every
fixture payload is collected HERE, from this test's own list rather than
the module's (a field the module forgets to classify must still fail), and
checked against the spool after every single hook, and against the spans
built from it.
"""
import json

from rius_cc import cursor_events, cursor_spans

from .cursor_fixtures import all_names, clock, payloads

MIN_CONTENT_LEN = 12

CONTENT_KEYS = ("prompt", "attachments", "text", "agent_message",
                "tool_input", "tool_output", "error_message", "command",
                "output", "result_json", "file_path", "edits", "task",
                "description", "summary", "modified_files", "user_email",
                "transcript_path", "agent_transcript_path")


def _leaves(value, out):
    if isinstance(value, str):
        if len(value.strip()) >= MIN_CONTENT_LEN:
            out.add(value.strip())
    elif isinstance(value, dict):
        for item in value.values():
            _leaves(item, out)
    elif isinstance(value, list):
        for item in value:
            _leaves(item, out)


def _secrets(events):
    out = set()
    for payload in events:
        for key in CONTENT_KEYS:
            if key in payload:
                _leaves(payload[key], out)
                if not isinstance(payload[key], str):
                    _leaves(json.dumps(payload[key]), out)
    return out


def _spool_blob(spool_dir):
    return "".join(p.read_text(encoding="utf-8") for p in spool_dir.glob("*.jsonl"))


def _span_blob(spans):
    return json.dumps([(s.name, s.status_message, s.attributes,
                        [(e[1], e[2]) for e in s.events]) for s in spans])


def test_no_fixture_content_ever_reaches_the_spool_with_capture_off(tmp_path):
    names = all_names()
    assert names, "no Cursor fixtures found -- this guard would be vacuous"
    for name in names:
        events = payloads(name)
        secrets = _secrets(events)
        assert secrets, name
        spool_dir = tmp_path / name
        tick = clock()
        for i, payload in enumerate(events):
            cursor_events.record(payload, str(spool_dir), False, 32768, clock=tick)
            blob = _spool_blob(spool_dir)
            for secret in secrets:
                assert secret not in blob, (
                    "%s: content %r reached the spool after hook %d (%s)"
                    % (name, secret[:40], i, payload["hook_event_name"]))

        cid = events[0]["conversation_id"]
        spooled = cursor_events.read_conversation(str(spool_dir), cid)
        ctx = cursor_spans.Ctx(cid, capture_content=False, max_attr_bytes=32768)
        for final_ns in (None, 1):
            built = _span_blob(cursor_spans.build(spooled, ctx, final_ns=final_ns))
            for secret in secrets:
                assert secret not in built, (name, secret[:40])


def test_the_guard_has_teeth(tmp_path):
    """With capture ON the same secrets do land in the spool, so the guard
    above is looking in the right place for the right strings."""
    for name in all_names():
        events = payloads(name)
        spool_dir = tmp_path / name
        tick = clock()
        for payload in events:
            cursor_events.record(payload, str(spool_dir), True, 32768, clock=tick)
        blob = _spool_blob(spool_dir)
        assert any(secret in blob for secret in _secrets(events)), name
