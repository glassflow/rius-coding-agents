"""A session killed before its SessionEnd has its trace closed by the next
SessionStart that uses the same key.

The backend counts a trace on the Agents/Users tabs only when every span of
it has a finished row, so a killed session's pending root hides it there for
good. The sweep must never send a trace's closing spans with a different key
than the one that opened it: that key may belong to another workspace.
"""
import json
import os
import pathlib

import pytest

import exporter
from rius_cc import config, spans, state

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
DEAD = "22222222-2222-2222-2222-222222222222"
NEW = "33333333-3333-3333-3333-333333333333"
KEY = "gf_deadbeefcafe0123.sig"
ENV = {"RIUS_API_KEY": KEY, "RIUS_ENDPOINT": "https://ingest.test"}
OTHER_ENV = {"RIUS_API_KEY": "gf_otherworkspace99.sig",
             "RIUS_ENDPOINT": "https://ingest.test"}
CONTENT_KEYS = ("input.value", "output.value")
HOUR_NS = 3600 * 10**9


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    (h / ".claude" / "rius").mkdir(parents=True)
    with open(config.path_rules_path(str(h)), "w") as fh:
        json.dump({"enabled_paths": ["/tmp"], "disabled_paths": []}, fh)
    return str(h)


@pytest.fixture
def sent(monkeypatch):
    """(resource, spans, key) per batch handed to the wire."""
    batches = []
    pending = {}

    def encode(resource, out):
        pending["batch"] = (resource, list(out))
        return b"x"

    def export(endpoint, key, body, timeout=5.0):
        resource, out = pending.pop("batch")
        batches.append((resource, out, key))
        return 200

    monkeypatch.setattr(exporter.otlp, "encode", encode)
    monkeypatch.setattr(exporter.otlp, "export", export)
    return batches


def _transcript(tmp_path, session_id, n_lines):
    """The tool_call fixture cut after `n_lines`: 2 leaves the turn and the
    tool open, as in a session killed mid-tool."""
    lines = (FIXTURES / "tool_call.jsonl").read_text().splitlines()[:n_lines]
    path = tmp_path / (session_id + ".jsonl")
    path.write_text("".join(line + "\n" for line in lines))
    return path


def _run(event, session_id, path, home, env=ENV):
    payload = {"session_id": session_id, "transcript_path": str(path),
               "cwd": "/tmp/proj", "hook_event_name": event}
    return exporter.run(event, payload, env, home)


def _kill_mid_tool(tmp_path, home, env=ENV):
    """A session that exports its open spans and is never heard from again."""
    path = _transcript(tmp_path, DEAD, 2)
    _run("PostToolUse", DEAD, path, home, env)
    return path


def _start_another(tmp_path, home, env=ENV):
    _run("SessionStart", NEW, _transcript(tmp_path, NEW, 0), home, env)


def _for(batches, session_id):
    trace = spans.trace_id_for(session_id)
    return [(resource, [s for s in out if s.trace_id == trace], key)
            for resource, out, key in batches
            if any(s.trace_id == trace for s in out)]


def _left_pending(batches):
    last = {}
    for _resource, out, _key in batches:
        for span in out:
            last[span.span_id] = span.pending
    return sorted(sid for sid, pending in last.items() if pending)


def _marker(home, session_id):
    return os.path.exists(state.open_marker_path(session_id, home))


def test_a_killed_session_is_closed_by_the_next_session_start(home, sent,
                                                              tmp_path):
    _kill_mid_tool(tmp_path, home)
    assert _left_pending(sent), "fixture should leave spans open"
    assert _marker(home, DEAD)

    _start_another(tmp_path, home)

    assert _left_pending(_for(sent, DEAD)) == []
    resource, closing, key = _for(sent, DEAD)[-1]
    root = spans.span_id_for("session:" + DEAD)
    assert [s for s in closing if s.span_id == root and not s.pending]
    assert key == KEY
    assert resource["service.instance.id"] == state.load(DEAD, home)["instance_id"]
    assert resource["cc.cwd"] == "/tmp/proj"
    assert not _marker(home, DEAD)
    assert state.load(DEAD, home)["finalized"] is True


def test_the_closed_trace_ends_when_the_session_was_last_seen(home, sent,
                                                             tmp_path):
    _kill_mid_tool(tmp_path, home)
    last_ns = state.load(DEAD, home)["last_ns"]

    _start_another(tmp_path, home)

    _resource, closing, _key = _for(sent, DEAD)[-1]
    assert {s.end_ns for s in closing} == {last_ns}


def test_the_closing_spans_carry_no_content(home, sent, tmp_path):
    _kill_mid_tool(tmp_path, home)
    assert state.load(DEAD, home)["open_turns"]["p1"]["text"] == "read a file"

    _start_another(tmp_path, home)

    _resource, closing, _key = _for(sent, DEAD)[-1]
    assert closing
    for span in closing:
        for key in CONTENT_KEYS:
            assert key not in span.attributes, (span.name, key)


def test_a_trace_opened_with_another_key_is_left_alone(home, sent, tmp_path):
    _kill_mid_tool(tmp_path, home, env=OTHER_ENV)
    before = len(_for(sent, DEAD))

    _start_another(tmp_path, home, env=ENV)

    assert len(_for(sent, DEAD)) == before
    assert all(key == OTHER_ENV["RIUS_API_KEY"]
               for _r, _s, key in _for(sent, DEAD))
    assert _marker(home, DEAD), "another key's session may still close it"


def test_the_same_key_against_another_endpoint_is_another_key(home, sent,
                                                             tmp_path):
    _kill_mid_tool(tmp_path, home)
    before = len(_for(sent, DEAD))

    _start_another(tmp_path, home,
                   env=dict(ENV, RIUS_ENDPOINT="https://elsewhere.test"))

    assert len(_for(sent, DEAD)) == before


def test_a_session_seen_within_the_cap_is_left_open(home, sent, tmp_path):
    _kill_mid_tool(tmp_path, home)
    last_ns = state.load(DEAD, home)["last_ns"]
    before = len(sent)
    cfg = config.resolve(NEW, "/tmp/proj", ENV, home)

    exporter.sweep_stale(cfg, NEW, home, now_ns=last_ns + 11 * HOUR_NS)
    assert len(sent) == before

    exporter.sweep_stale(cfg, NEW, home, now_ns=last_ns + 13 * HOUR_NS)
    assert _left_pending(_for(sent, DEAD)) == []


def test_a_session_whose_pinger_is_alive_is_left_open(home, sent, tmp_path):
    _kill_mid_tool(tmp_path, home)
    with open(os.path.join(state.state_dir(home),
                           DEAD + ".heartbeat.pid"), "w") as fh:
        fh.write(str(os.getpid()))
    before = len(sent)

    _start_another(tmp_path, home)

    assert len(_for(sent, DEAD)) == len(_for(sent[:before], DEAD))
    assert _marker(home, DEAD)


def test_a_session_holding_its_lock_is_left_open(home, sent, tmp_path):
    _kill_mid_tool(tmp_path, home)
    before = len(sent)

    with state.session_lock(DEAD, home) as got:
        assert got
        _start_another(tmp_path, home)

    assert len(_for(sent, DEAD)) == len(_for(sent[:before], DEAD))


def test_a_failed_close_is_retried_by_a_later_session_start(home, sent,
                                                            monkeypatch,
                                                            tmp_path):
    _kill_mid_tool(tmp_path, home)
    real_export = exporter.otlp.export
    monkeypatch.setattr(exporter.otlp, "export",
                        lambda ep, key, body, timeout=5.0: 503)
    _start_another(tmp_path, home)
    assert _marker(home, DEAD)
    assert state.trace_is_open(state.load(DEAD, home))

    monkeypatch.setattr(exporter.otlp, "export", real_export)
    _start_another(tmp_path, home)
    assert _left_pending(_for(sent, DEAD)) == []
    assert not _marker(home, DEAD)


def test_a_session_that_ended_leaves_no_marker(home, sent, tmp_path):
    path = _kill_mid_tool(tmp_path, home)
    assert _marker(home, DEAD)

    _run("SessionEnd", DEAD, path, home)

    assert not _marker(home, DEAD)


def test_a_disabled_session_closed_on_session_end_leaves_no_marker(
        home, sent, tmp_path):
    path = _kill_mid_tool(tmp_path, home)
    with open(config.path_rules_path(home), "w") as fh:
        json.dump({"enabled_paths": ["/tmp"],
                   "disabled_paths": ["/tmp/proj"]}, fh)

    _run("SessionEnd", DEAD, path, home)

    assert not _marker(home, DEAD)


def test_a_marker_for_a_closed_trace_is_cleared(home, sent, tmp_path):
    path = _kill_mid_tool(tmp_path, home)
    st = state.load(DEAD, home)
    st["finalized"] = True
    state.save(DEAD, home, st)
    before = len(sent)

    _start_another(tmp_path, home)

    assert len(_for(sent, DEAD)) == len(_for(sent[:before], DEAD))
    assert not _marker(home, DEAD)
    assert path.exists()


def test_the_key_itself_never_reaches_the_state_file(home, sent, tmp_path):
    _kill_mid_tool(tmp_path, home)

    text = pathlib.Path(state.state_path(DEAD, home)).read_text()

    assert state.load(DEAD, home)["key_fingerprint"]
    assert KEY not in text
    assert KEY.split(".")[0] not in text


def test_a_session_whose_claude_code_is_alive_is_left_open(home, sent,
                                                          tmp_path):
    """Idle, not dead: past its 12h cap the pinger is gone either way."""
    _kill_mid_tool(tmp_path, home)
    st = state.load(DEAD, home)
    st["cc_pid"] = os.getpid()
    state.save(DEAD, home, st)
    before = len(sent)

    _start_another(tmp_path, home)

    assert len(_for(sent, DEAD)) == len(_for(sent[:before], DEAD))
    assert state.trace_is_open(state.load(DEAD, home))


def test_the_trace_stays_with_the_key_that_opened_it(home, sent, tmp_path):
    """Signing in to another workspace mid-session must not hand the trace
    to that workspace's key."""
    path = _kill_mid_tool(tmp_path, home)
    with open(path, "a") as fh:
        fh.write((FIXTURES / "tool_call.jsonl").read_text().splitlines()[2]
                 + "\n")
    _run("PostToolUse", DEAD, path, home, OTHER_ENV)
    assert _for(sent, DEAD)[-1][2] == OTHER_ENV["RIUS_API_KEY"]
    before = len(_for(sent, DEAD))

    _start_another(tmp_path, home, env=OTHER_ENV)
    assert len(_for(sent, DEAD)) == before

    _start_another(tmp_path, home, env=ENV)
    _resource, _closing, key = _for(sent, DEAD)[-1]
    assert key == KEY
    assert _left_pending(_for(sent, DEAD)) == []


def test_a_trace_no_export_was_accepted_for_has_no_owner(home, monkeypatch,
                                                         sent, tmp_path):
    monkeypatch.setattr(exporter.otlp, "export",
                        lambda ep, key, body, timeout=5.0: 403)
    _kill_mid_tool(tmp_path, home)

    assert not state.load(DEAD, home).get("key_fingerprint")
    assert not _marker(home, DEAD)
