"""exporter.run_cursor: rebuild the conversation from its spool, send only
what changed since the last accepted export, close what is left open."""
import os

import pytest

import exporter
from rius_cc import agent, config, cursor_events, cursor_export, otlp, state
from tests import cursor_fixtures

CID = "c0ffee00-0000-4000-8000-000000000001"
ENV = {"RIUS_API_KEY": "glassflow_k", "RIUS_ENDPOINT": "https://ingest.test",
       "RIUS_CURSOR_ENABLED": "true"}


@pytest.fixture(autouse=True)
def cursor():
    with agent.using(agent.CURSOR):
        yield


class Receiver:
    """Every batch the exporter POSTs; `status` is the receiver's answer."""

    def __init__(self):
        self.status = 200
        self.batches = []

    def encode(self, resource, spans):
        self.batches.append({"resource": resource, "spans": list(spans)})
        return b"x"

    def export(self, *args, **kwargs):
        return self.status


@pytest.fixture
def sent(monkeypatch):
    receiver = Receiver()
    monkeypatch.setattr(otlp, "encode", receiver.encode)
    monkeypatch.setattr(otlp, "export", receiver.export)
    return receiver


def _spool_until(home, name, last_event=None):
    """Spool a fixture's payloads up to and including `last_event`'s index."""
    sdir = cursor_export.spool_dir(str(home))
    tick = cursor_fixtures.clock()
    payloads = cursor_fixtures.payloads(name)
    stop = len(payloads) if last_event is None else last_event + 1
    for payload in payloads[:stop]:
        cursor_events.record(payload, sdir, True, 32768, clock=tick)
    return payloads


def _run(home, event, env=ENV, cid=CID):
    return exporter.run_cursor(event, {"conversation_id": cid, "cwd": "/w"},
                               env, str(home))


def test_session_start_sends_the_pending_root(tmp_path, sent):
    _spool_until(tmp_path, "docs_session", 0)
    assert _run(tmp_path, "sessionStart") == 1
    root = sent.batches[0]["spans"][0]
    assert root.name == "cursor session" and root.pending
    assert sent.batches[0]["resource"]["service.name"] == "cursor"


def test_unchanged_spans_are_not_sent_again(tmp_path, sent):
    _spool_until(tmp_path, "docs_session", 0)
    _run(tmp_path, "sessionStart")
    assert _run(tmp_path, "sessionStart") == 0


def test_a_finished_turn_sends_only_what_changed(tmp_path, sent):
    payloads = _spool_until(tmp_path, "docs_session", 0)
    _run(tmp_path, "sessionStart")
    first_stop = [p["hook_event_name"] for p in payloads].index("stop")
    sdir = cursor_export.spool_dir(str(tmp_path))
    for i, payload in enumerate(payloads[1:first_stop + 1], start=1):
        cursor_events.record(payload, sdir, True, 32768,
                             clock=lambda i=i: cursor_fixtures.BASE_NS + i)
    _run(tmp_path, "stop")
    spans = sent.batches[1]["spans"]
    assert "cursor session" not in [s.name for s in spans]
    assert all(not s.pending for s in spans if s.kind_oi != "AGENT")
    assert "turn" in [s.name for s in spans]


def test_session_end_closes_everything(tmp_path, sent):
    _spool_until(tmp_path, "docs_inflight")
    _run(tmp_path, "sessionEnd", cid="c0ffee00-0000-4000-8000-000000000003")
    assert sent.batches and not any(s.pending for s in sent.batches[0]["spans"])
    st = state.load("c0ffee00-0000-4000-8000-000000000003", str(tmp_path))
    assert st["finalized"] and not state.trace_is_open(st)


def test_a_retryable_failure_sends_the_same_spans_again(tmp_path, sent):
    _spool_until(tmp_path, "docs_session", 0)
    sent.status = 503
    _run(tmp_path, "sessionStart")
    sent.status = 200
    assert _run(tmp_path, "sessionStart") == 1
    assert state.load(CID, str(tmp_path))["consecutive_export_failures"] == 0


def test_an_accepted_export_marks_the_trace_open(tmp_path, sent):
    _spool_until(tmp_path, "docs_session", 0)
    _run(tmp_path, "sessionStart")
    assert state.open_marked_sessions(str(tmp_path)) == [CID]
    assert state.load(CID, str(tmp_path))["key_fingerprint"]


def test_disabled_mid_session_closes_without_resending_content(tmp_path, sent):
    payloads = _spool_until(tmp_path, "docs_session", 14)
    assert payloads[14]["hook_event_name"] == "stop"
    _run(tmp_path, "stop")
    finished = {s.span_id for s in sent.batches[0]["spans"] if not s.pending}
    off = dict(ENV, RIUS_CURSOR_ENABLED="false")
    assert _run(tmp_path, "stop", env=off) == 0
    _run(tmp_path, "sessionEnd", env=off)
    closing = sent.batches[-1]["spans"]
    assert closing and not ({s.span_id for s in closing} & finished)
    assert not any("input.value" in s.attributes or "output.value"
                   in s.attributes for s in closing)
    assert not any(s.pending for s in closing)


def test_disabled_with_no_open_trace_sends_nothing(tmp_path, sent):
    _spool_until(tmp_path, "docs_session")
    off = dict(ENV, RIUS_CURSOR_ENABLED="false")
    assert _run(tmp_path, "sessionEnd", env=off) == 0
    assert sent.batches == []


def _stale(tmp_path, sent, idle_s):
    """An open trace last seen `idle_s` ago; returns what the sweep sent."""
    _spool_until(tmp_path, "docs_inflight")
    cid = "c0ffee00-0000-4000-8000-000000000003"
    _run(tmp_path, "stop", cid=cid)
    before = len(sent.batches)
    last = state.load(cid, str(tmp_path))["last_ns"]
    cfg = config.resolve(CID, "/w", ENV, str(tmp_path))
    exporter.sweep_stale_cursor(cfg, CID, str(tmp_path),
                                now_ns=last + idle_s * 10**9)
    return sent.batches[before:], cid


def test_the_sweep_closes_a_conversation_that_never_ended(tmp_path, sent):
    swept, cid = _stale(tmp_path, sent, exporter.STALE_AFTER_S + 1)
    assert swept and not any(s.pending for s in swept[0]["spans"])
    assert not state.trace_is_open(state.load(cid, str(tmp_path)))
    assert state.open_marked_sessions(str(tmp_path)) == []


def test_the_sweep_leaves_an_idle_conversation_alone(tmp_path, sent):
    swept, cid = _stale(tmp_path, sent, 60)
    assert swept == []
    assert state.trace_is_open(state.load(cid, str(tmp_path)))


def test_state_lives_in_the_cursor_home(tmp_path, sent):
    _spool_until(tmp_path, "docs_session", 0)
    _run(tmp_path, "sessionStart")
    assert (tmp_path / ".cursor" / "rius" / "state" / (CID + ".json")).exists()
    assert not (tmp_path / ".claude").exists()


def test_claude_code_runs_never_take_the_cursor_path(tmp_path, monkeypatch):
    monkeypatch.setattr(exporter, "run_cursor",
                        lambda *a: pytest.fail("cursor path taken"))
    with agent.using(agent.CLAUDE_CODE):
        assert exporter.run("Stop", {}, {}, str(tmp_path)) == 0


def _age(path, days):
    old = os.path.getmtime(path) - days * 24 * 3600
    os.utime(path, (old, old))


def _spool_files(sdir):
    return sorted(os.listdir(sdir))


def test_a_spool_idle_past_retention_is_deleted(tmp_path):
    sdir = cursor_export.spool_dir(str(tmp_path))
    _spool_until(tmp_path, "docs_session")
    for name in _spool_files(sdir):
        _age(os.path.join(sdir, name), 8)
    assert cursor_export.prune(sdir, []) > 0
    assert _spool_files(sdir) == []


def test_a_recent_spool_is_kept(tmp_path):
    sdir = cursor_export.spool_dir(str(tmp_path))
    _spool_until(tmp_path, "docs_session")
    before = _spool_files(sdir)
    assert cursor_export.prune(sdir, []) == 0
    assert _spool_files(sdir) == before


def test_an_open_trace_keeps_its_spool_and_its_subagents(tmp_path):
    sdir = cursor_export.spool_dir(str(tmp_path))
    for payload in _spool_until(tmp_path, "docs_session"):
        if payload.get("hook_event_name") == "subagentStart":
            cursor_export.link_subagent(sdir, payload["subagent_id"], CID)
    for name in _spool_files(sdir):
        _age(os.path.join(sdir, name), 30)
    before = _spool_files(sdir)
    assert cursor_export.prune(sdir, [CID]) == 0
    assert _spool_files(sdir) == before


def test_session_start_prunes_old_spools(tmp_path, sent):
    sdir = cursor_export.spool_dir(str(tmp_path))
    _spool_until(tmp_path, "docs_session")
    for name in _spool_files(sdir):
        _age(os.path.join(sdir, name), 8)
    other = "c0ffee00-0000-4000-8000-000000000002"
    cursor_events.record({"conversation_id": other,
                          "hook_event_name": "sessionStart"}, sdir, True, 100)
    _run(tmp_path, "sessionStart", cid=other)
    assert _spool_files(sdir) == [other + cursor_events.SPOOL_SUFFIX]


def test_rius_service_name_renames_the_service(tmp_path, sent):
    _spool_until(tmp_path, "docs_session", 0)
    _run(tmp_path, "sessionStart", env=dict(ENV, RIUS_SERVICE_NAME="cursor-ci"))
    assert sent.batches[-1]["resource"]["service.name"] == "cursor-ci"


def test_the_default_service_is_cursor(tmp_path, sent):
    _spool_until(tmp_path, "docs_session", 0)
    _run(tmp_path, "sessionStart")
    assert sent.batches[-1]["resource"]["service.name"] == "cursor"
