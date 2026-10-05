import json

import pytest

import exporter
from rius_cc import config, state
from tests.signed_in import TEST_ENDPOINT, sign_in


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    (h / ".claude" / "rius").mkdir(parents=True)
    sign_in(h)
    with open(config.path_rules_path(str(h)), "w") as fh:
        json.dump({"enabled_paths": ["/tmp"]}, fh)
    return str(h)


@pytest.fixture
def captured(monkeypatch):
    sent = []
    monkeypatch.setattr(exporter.otlp, "export",
                        lambda ep, key, body, timeout=5.0: sent.append((ep, key, body)) or 200)
    return sent


def _payload(fixtures_dir, name, session_id, event, cwd="/tmp/proj"):
    return {"session_id": session_id, "transcript_path": str(fixtures_dir / name),
            "cwd": cwd, "hook_event_name": event}


ENV = {}


def test_exports_spans_and_advances_offset(home, captured, fixtures_dir):
    sid = "11111111-1111-1111-1111-111111111111"
    n = exporter.run("PostToolUse", _payload(fixtures_dir, "simple.jsonl", sid, "PostToolUse"), ENV, home)
    assert n > 0
    assert len(captured) == 1
    assert captured[0][0] == TEST_ENDPOINT
    assert state.load(sid, home)["offset"] > 0


def test_second_run_with_no_new_lines_sends_nothing(home, captured, fixtures_dir):
    sid = "11111111-1111-1111-1111-111111111111"
    p = _payload(fixtures_dir, "simple.jsonl", sid, "PostToolUse")
    exporter.run("PostToolUse", p, ENV, home)
    before = len(captured)
    assert exporter.run("PostToolUse", p, ENV, home) == 0
    assert len(captured) == before


def test_disabled_folder_exports_nothing(home, captured, fixtures_dir):
    sid = "11111111-1111-1111-1111-111111111111"
    p = _payload(fixtures_dir, "simple.jsonl", sid, "PostToolUse", cwd="/elsewhere")
    assert exporter.run("PostToolUse", p, ENV, home) == 0
    assert captured == []


def test_session_end_closes_the_root(home, captured, fixtures_dir):
    sid = "11111111-1111-1111-1111-111111111111"
    exporter.run("PostToolUse", _payload(fixtures_dir, "simple.jsonl", sid, "PostToolUse"), ENV, home)
    captured.clear()
    n = exporter.run("SessionEnd", _payload(fixtures_dir, "simple.jsonl", sid, "SessionEnd"), ENV, home)
    assert n >= 1          # at minimum, the final root span


def test_never_raises_on_a_missing_transcript(home, captured):
    p = {"session_id": "s9", "transcript_path": "/nope/missing.jsonl",
         "cwd": "/tmp/proj", "hook_event_name": "Stop"}
    assert exporter.run("Stop", p, ENV, home) == 0


def test_never_raises_on_a_garbage_payload(home, captured):
    assert exporter.run("Stop", {}, ENV, home) == 0


def test_final_events_wait_briefly_for_the_lock(home, captured, fixtures_dir, monkeypatch):
    """I4: losing the lock is free for PostToolUse -- a later hook re-reads
    the same lines. It is not free for SessionEnd or Stop: nothing runs
    after them, so the root span and any open turn stay pending forever."""
    import contextlib
    seen = []
    real = state.session_lock

    @contextlib.contextmanager
    def recording(session_id, h, block_timeout=0.0, **kw):
        seen.append(block_timeout)
        with real(session_id, h, block_timeout=block_timeout, **kw) as got:
            yield got

    monkeypatch.setattr(exporter.state, "session_lock", recording)
    sid = "11111111-1111-1111-1111-111111111111"
    for event in ("PostToolUse", "UserPromptSubmit"):
        exporter.run(event, _payload(fixtures_dir, "simple.jsonl", sid, event), ENV, home)
    assert seen == [0.0, 0.0]

    seen[:] = []
    for event in ("Stop", "SessionEnd"):
        exporter.run(event, _payload(fixtures_dir, "simple.jsonl", sid, event), ENV, home)
    assert seen == [exporter.FINAL_EVENT_LOCK_TIMEOUT_S] * 2
    assert all(t > 0 for t in seen)


def test_lock_contention_is_a_no_op(home, captured, fixtures_dir):
    sid = "11111111-1111-1111-1111-111111111111"
    with state.session_lock(sid, home):
        n = exporter.run("PostToolUse",
                         _payload(fixtures_dir, "simple.jsonl", sid, "PostToolUse"), ENV, home)
    assert n == 0
    assert captured == []


def test_api_key_never_appears_in_the_log(home, captured, fixtures_dir):
    sid = "11111111-1111-1111-1111-111111111111"
    env = dict(ENV, RIUS_CLAUDE_DEBUG="true")
    exporter.run("PostToolUse", _payload(fixtures_dir, "simple.jsonl", sid, "PostToolUse"), env, home)
    import pathlib
    logs = list(pathlib.Path(home, ".claude", "rius", "log").glob("*.log"))
    assert logs, "expected a debug log file to be written when RIUS_CLAUDE_DEBUG=true"
    text = "\n".join(log.read_text() for log in logs)
    assert text.strip(), "expected the debug log to be non-empty"
    assert "glassflow_k" not in text
    assert config.redact("glassflow_k") in text


def test_spans_exported_counter_accumulates_on_success_only(home, captured, fixtures_dir,
                                                            monkeypatch):
    sid = "11111111-1111-1111-1111-111111111111"
    p = _payload(fixtures_dir, "simple.jsonl", sid, "PostToolUse")
    n1 = exporter.run("PostToolUse", p, ENV, home)
    st = state.load(sid, home)
    assert st["spans_exported"] == n1 > 0

    # SessionEnd on the same transcript produces further spans (e.g. the
    # closing root span) and a second successful export.
    n2 = exporter.run("SessionEnd", _payload(fixtures_dir, "simple.jsonl", sid, "SessionEnd"), ENV, home)
    st = state.load(sid, home)
    assert st["spans_exported"] == n1 + n2

    # A failed export must not increment the counter. monkeypatch, not a bare
    # module assignment: the old version leaked a broken exporter into every
    # test that ran after it.
    before = st["spans_exported"]
    monkeypatch.setattr(exporter.otlp, "export",
                        lambda ep, key, body, timeout=5.0: 500)
    exporter.run("PostToolUse", _payload(fixtures_dir, "tool_call.jsonl", sid, "PostToolUse"), ENV, home)
    st = state.load(sid, home)
    assert st["spans_exported"] == before


def test_spans_keep_flowing_after_a_compaction(home, captured, fixtures_dir, tmp_path):
    """End to end for the offset>size reset: the stored offset outlives the
    transcript it referred to."""
    sid = "11111111-1111-1111-1111-111111111111"
    t = tmp_path / "t.jsonl"
    t.write_bytes((fixtures_dir / "tool_call.jsonl").read_bytes())
    p = {"session_id": sid, "transcript_path": str(t), "cwd": "/tmp/proj"}
    assert exporter.run("PostToolUse", p, ENV, home) > 0
    stale = state.load(sid, home)["offset"]
    assert stale > 0

    short = (fixtures_dir / "compacted.jsonl").read_bytes()
    t.write_bytes(short)
    assert len(short) < stale
    n = exporter.run("PostToolUse", p, ENV, home)
    assert n > 0, "session went silent after the transcript was compacted"
    assert state.load(sid, home)["offset"] == len(short)


def test_a_subagent_session_exports_without_an_agent_span_per_subagent(
        home, captured, fixtures_dir):
    sid = "44444444-4444-4444-4444-444444444444"
    p = _payload(fixtures_dir, "subagent.jsonl", sid, "PostToolUse")
    assert exporter.run("PostToolUse", p, ENV, home) > 0
    assert state.load(sid, home)["open_task_spans"] == []


def test_unparseable_transcript_lines_are_counted_and_logged_once(home, captured, tmp_path):
    """I3: a transcript the parser cannot read at all looks EXACTLY like an
    idle session -- zero spans, zero errors, a happy /rius status."""
    sid = "11111111-1111-1111-1111-111111111111"
    t = tmp_path / "bad.jsonl"
    t.write_bytes(b'{"type":"user","uuid":"u1","timestamp":"not-a-timestamp"}\n'
                  b'{"type":"user","uuid":"u2","timestamp":"also-bad"}\n')
    p = {"session_id": sid, "transcript_path": str(t), "cwd": "/tmp/proj"}
    env = dict(ENV, RIUS_CLAUDE_DEBUG="true")

    exporter.run("PostToolUse", p, env, home)
    st = state.load(sid, home)
    assert st["lines_skipped"] == 2

    import pathlib
    logs = list(pathlib.Path(home, ".claude", "rius", "log").glob("*.log"))
    text = "\n".join(log.read_text() for log in logs)
    assert "skipped" in text.lower()
    assert text.lower().count("unparseable") == 1, "should log once, not per line"

    # a second batch of bad lines accumulates the count but does not re-log
    with open(str(t), "ab") as fh:
        fh.write(b'{"type":"user","uuid":"u3","timestamp":"bad-again"}\n')
    exporter.run("PostToolUse", p, env, home)
    assert state.load(sid, home)["lines_skipped"] == 3
    text = "\n".join(log.read_text() for log in
                     pathlib.Path(home, ".claude", "rius", "log").glob("*.log"))
    assert text.lower().count("unparseable") == 1


# --- I2: an export failure must not grow an unbounded batch in silence ----

def _failing(monkeypatch, status):
    calls = []

    def fake(ep, key, body, timeout=5.0):
        calls.append(body)
        return status

    monkeypatch.setattr(exporter.otlp, "export", fake)
    return calls


def test_permanent_4xx_advances_the_offset_and_records_why(home, monkeypatch, fixtures_dir):
    """A key without the scope is permanent. Holding the offset means every later
    hook re-reads from the same place and re-encodes an ever larger batch,
    forever: growing CPU, growing payload, nothing exported, nothing said."""
    sid = "11111111-1111-1111-1111-111111111111"
    sent = _failing(monkeypatch, 403)
    exporter.run("PostToolUse", _payload(fixtures_dir, "simple.jsonl", sid, "PostToolUse"),
                 ENV, home)

    st = state.load(sid, home)
    assert st["offset"] > 0, "a permanent failure must not re-read the same lines forever"
    assert st["spans_exported"] == 0
    err = st["last_export_error"]
    assert err["status"] == 403
    assert "/rius:login" in err["reason"]
    assert err["at"]

    # and the next batch is genuinely a NEW batch, not the same one again
    exporter.run("PostToolUse", _payload(fixtures_dir, "simple.jsonl", sid, "PostToolUse"),
                 ENV, home)
    assert len(sent) == 1, "nothing new to send, so nothing should have been sent"


def test_transient_5xx_holds_the_offset_but_still_records_why(home, monkeypatch, fixtures_dir):
    sid = "11111111-1111-1111-1111-111111111111"
    _failing(monkeypatch, 503)
    exporter.run("PostToolUse", _payload(fixtures_dir, "simple.jsonl", sid, "PostToolUse"),
                 ENV, home)
    st = state.load(sid, home)
    assert st["offset"] == 0, "a transient failure should retry the same lines"
    assert st["consecutive_export_failures"] == 1
    assert st["last_export_error"]["status"] == 503


def test_401_is_transient_because_a_fresh_key_takes_seconds_to_go_live(
        home, monkeypatch, fixtures_dir):
    sid = "11111111-1111-1111-1111-111111111111"
    _failing(monkeypatch, 401)
    exporter.run("PostToolUse", _payload(fixtures_dir, "simple.jsonl", sid, "PostToolUse"),
                 ENV, home)
    st = state.load(sid, home)
    assert st["offset"] == 0, "the spans must survive until the key is live"
    assert "/rius:login" in st["last_export_error"]["reason"]


def test_429_is_treated_as_transient(home, monkeypatch, fixtures_dir):
    sid = "11111111-1111-1111-1111-111111111111"
    _failing(monkeypatch, 429)
    exporter.run("PostToolUse", _payload(fixtures_dir, "simple.jsonl", sid, "PostToolUse"),
                 ENV, home)
    assert state.load(sid, home)["offset"] == 0


def test_repeated_transient_failures_are_capped(home, monkeypatch, fixtures_dir):
    """Otherwise an endpoint that 500s for an hour has the same unbounded
    growth as a permanent failure, just more slowly."""
    sid = "11111111-1111-1111-1111-111111111111"
    _failing(monkeypatch, 500)
    p = _payload(fixtures_dir, "simple.jsonl", sid, "PostToolUse")
    for _ in range(exporter.MAX_CONSECUTIVE_EXPORT_FAILURES):
        assert state.load(sid, home)["offset"] == 0
        exporter.run("PostToolUse", p, ENV, home)
    st = state.load(sid, home)
    assert st["offset"] > 0, "gave up but kept re-reading the same lines"
    assert st["consecutive_export_failures"] == 0     # fresh budget for the next batch


def test_a_transport_failure_is_transient_and_named(home, monkeypatch, fixtures_dir):
    sid = "11111111-1111-1111-1111-111111111111"
    _failing(monkeypatch, 0)
    exporter.run("PostToolUse", _payload(fixtures_dir, "simple.jsonl", sid, "PostToolUse"),
                 ENV, home)
    st = state.load(sid, home)
    assert st["offset"] == 0
    assert "network" in st["last_export_error"]["reason"]


def test_skipped_lines_are_logged_once_even_across_export_failures(home, monkeypatch, tmp_path):
    sid = "11111111-1111-1111-1111-111111111111"
    t = tmp_path / "bad.jsonl"
    t.write_bytes(b'{"type":"user","uuid":"u1","timestamp":"bad"}\n'
                  b'{"type":"user","uuid":"u2","timestamp":"2026-09-22T10:00:00.000Z"}\n')
    p = {"session_id": sid, "transcript_path": str(t), "cwd": "/tmp/proj"}
    env = dict(ENV, RIUS_CLAUDE_DEBUG="true")
    _failing(monkeypatch, 503)
    for _ in range(3):
        exporter.run("PostToolUse", p, env, home)
    import pathlib
    text = "\n".join(f.read_text() for f in
                     pathlib.Path(home, ".claude", "rius", "log").glob("*.log"))
    assert text.lower().count("unparseable") == 1


def test_a_failure_does_not_persist_half_built_span_state(home, monkeypatch, fixtures_dir):
    """Recording the error must not smuggle the span builder's mutations
    into state: the offset has not moved, so those lines get rebuilt."""
    sid = "11111111-1111-1111-1111-111111111111"
    _failing(monkeypatch, 503)
    exporter.run("PostToolUse", _payload(fixtures_dir, "tool_call.jsonl", sid, "PostToolUse"),
                 ENV, home)
    st = state.load(sid, home)
    assert st["root_started"] is False
    assert st["open_tools"] == {}


def test_a_later_success_clears_the_recorded_error(home, captured, monkeypatch, fixtures_dir):
    sid = "11111111-1111-1111-1111-111111111111"
    _failing(monkeypatch, 503)
    p = _payload(fixtures_dir, "simple.jsonl", sid, "PostToolUse")
    exporter.run("PostToolUse", p, ENV, home)
    assert state.load(sid, home)["last_export_error"]

    monkeypatch.setattr(exporter.otlp, "export",
                        lambda ep, key, body, timeout=5.0: 200)
    exporter.run("PostToolUse", p, ENV, home)
    st = state.load(sid, home)
    assert not st.get("last_export_error")
    assert st["consecutive_export_failures"] == 0
    assert st["spans_exported"] > 0


def test_instance_id_is_minted_on_session_start_even_with_no_spans(home, captured):
    sid = "22222222-2222-2222-2222-222222222222"
    p = {"session_id": sid, "transcript_path": "/nope/missing.jsonl",
         "cwd": "/tmp/proj", "hook_event_name": "SessionStart"}
    exporter.run("SessionStart", p, ENV, home)
    st = state.load(sid, home)
    assert st.get("instance_id")
    assert captured == []


def test_instance_id_is_stable_across_invocations(home, captured, fixtures_dir):
    sid = "11111111-1111-1111-1111-111111111111"
    p = {"session_id": sid, "transcript_path": "/nope/missing.jsonl",
         "cwd": "/tmp/proj", "hook_event_name": "SessionStart"}
    exporter.run("SessionStart", p, ENV, home)
    first = state.load(sid, home)["instance_id"]

    exporter.run("PostToolUse", _payload(fixtures_dir, "simple.jsonl", sid, "PostToolUse"), ENV, home)
    second = state.load(sid, home)["instance_id"]
    assert first == second


# --- subagent drilldown (N2) --------------------------------------------

SUB_SID = "55555555-5555-5555-5555-555555555555"


def _sub_payload(fixtures_dir, event):
    return {"session_id": SUB_SID,
            "transcript_path": str(fixtures_dir / "subagent_files"
                                   / (SUB_SID + ".jsonl")),
            "cwd": "/tmp/proj", "hook_event_name": event}


def test_subagent_spans_reach_the_wire(home, captured, fixtures_dir):
    """58% of the tokens in the acceptance session were inside subagents,
    written to their own transcript files. The exporter has to open them."""
    from rius_cc import spans as _spans

    n = exporter.run("Stop", _sub_payload(fixtures_dir, "Stop"), ENV, home)
    assert n > 0
    body = captured[0][2]
    # span ids go on the wire as raw bytes, not as their hex spelling
    assert bytes.fromhex(_spans.span_id_for("subagent:agent-aaa111")) in body
    assert bytes.fromhex(_spans.span_id_for("subagent:agent-bbb222")) in body
    assert b"gen_ai.agent.name" in body and b"general-purpose" in body
    assert b"claude-haiku-4-5-20251001" in body   # the subagent's own model

    st = state.load(SUB_SID, home)
    assert st["sub_offsets"]["agent-aaa111"] > 0
    assert st["sub_offsets"]["agent-bbb222"] > 0
    assert st["sub_links"]["toolu_agent1"]["agent_id"] == "agent-aaa111"


def test_a_second_run_re_exports_no_subagent_span(home, captured, fixtures_dir):
    """Per-agent offsets: a subagent's spans stream once, like the main
    transcript's."""
    exporter.run("PostToolUse", _sub_payload(fixtures_dir, "PostToolUse"), ENV, home)
    captured.clear()
    assert exporter.run("PostToolUse", _sub_payload(fixtures_dir, "PostToolUse"),
                        ENV, home) == 0
    assert captured == []


def test_a_transient_failure_rewinds_the_subagent_offsets(home, captured,
                                                          fixtures_dir,
                                                          monkeypatch):
    """The main offset is rewound on a retryable failure so the same lines
    are rebuilt. Subagent offsets are part of that same state and must rewind
    with it, or the retry re-sends the main spans and drops the subagent's."""
    monkeypatch.setattr(exporter.otlp, "export",
                        lambda ep, key, body, timeout=5.0: 503)
    exporter.run("PostToolUse", _sub_payload(fixtures_dir, "PostToolUse"), ENV, home)
    st = state.load(SUB_SID, home)
    assert st["offset"] == 0
    assert st["sub_offsets"] == {}
    assert st["consecutive_export_failures"] == 1



def _transcript_outside_git(tmp_path, fixtures_dir):
    lines = []
    for line in (fixtures_dir / "simple.jsonl").read_text().splitlines():
        record = json.loads(line)
        record.pop("gitBranch", None)
        lines.append(json.dumps(record))
    path = tmp_path / "no_git.jsonl"
    path.write_text("\n".join(lines) + "\n")
    return str(path)


def _resource_attrs_sent(monkeypatch, home, transcript, events):
    seen = []
    real_encode = exporter.otlp.encode
    monkeypatch.setattr(exporter.otlp, "encode",
                        lambda attrs, out: seen.append(dict(attrs)) or real_encode(attrs, out))
    monkeypatch.setattr(exporter.otlp, "export", lambda *_a, **_kw: 200)
    sid = "12121212-1212-1212-1212-121212121212"
    for event in events:
        payload = {"session_id": sid, "transcript_path": transcript,
                   "cwd": "/tmp/proj", "hook_event_name": event}
        exporter.run(event, payload, ENV, home, "inst-1")
    return seen


def test_unknown_resource_values_are_left_out_not_sent_empty(
        home, tmp_path, fixtures_dir, monkeypatch):
    """A folder outside git has no branch: the key is absent, not "", or the
    console's attribute catalog fills with empty values."""
    seen = _resource_attrs_sent(monkeypatch, home,
                                _transcript_outside_git(tmp_path, fixtures_dir),
                                ("PostToolUse", "SessionEnd"))
    assert len(seen) >= 2
    for attrs in seen:
        assert "" not in attrs.values() and None not in attrs.values()
        assert "cc.git_branch" not in attrs
        assert attrs["service.instance.id"] == "inst-1"


def test_known_resource_values_are_still_sent(home, fixtures_dir, monkeypatch):
    seen = _resource_attrs_sent(monkeypatch, home,
                                str(fixtures_dir / "simple.jsonl"), ("PostToolUse",))
    assert seen[0]["cc.git_branch"] == "main"
    assert seen[0]["cc.cwd"] == "/tmp/proj"
