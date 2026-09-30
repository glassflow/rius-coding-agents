"""A conversation Claude Code moves to a new session id stays ONE trace.

When Claude Code 2.1.x sends a session to the background, its daemon starts
the conversation under a NEW session id: it appends a `continued-in` record
to the old transcript, naming the new id, and writes a new transcript that
opens with a copy of the whole history -- the same entry uuids, a new
sessionId and new promptIds. The old process gets no SessionEnd.

The rule is the resume rule: the same conversation continuing is the same
trace; only a genuinely new session gets a new trace. So the new id skips
the copied history, emits its work under the conversation's trace and root
(a new instance, like a resumed process), and owns closing that root. The
old id hands everything over at the switch and never closes the root.
"""
import json
import os

import pytest

import exporter
from rius_cc import config, spans, state

OLD = "aaaaaaaa-0000-0000-0000-000000000001"
NEW = "bbbbbbbb-0000-0000-0000-000000000002"
NEWER = "cccccccc-0000-0000-0000-000000000003"
ENV = {"RIUS_API_KEY": "glassflow_k", "RIUS_ENDPOINT": "https://ingest.test"}
SWITCH_TS = "2026-09-30T10:05:00.000Z"
TRACE = spans.trace_id_for(OLD)
ROOT = spans.span_id_for("session:" + OLD)


def _ns(ts):
    return exporter.transcript._timestamp_ns(ts)


def _row(sid, **kw):
    row = {"isSidechain": False, "sessionId": sid, "cwd": "/tmp/proj",
           "gitBranch": "main", "version": "2.1.284"}
    row.update(kw)
    return row


def _assistant(sid, uuid, ts, mid, content, out, stop="end_turn", side=False):
    return _row(sid, type="assistant", uuid=uuid, parentUuid=None, timestamp=ts,
                isSidechain=side,
                message={"id": mid, "role": "assistant", "model": "claude-opus-5",
                         "stop_reason": stop, "content": content,
                         "usage": {"input_tokens": 2, "output_tokens": out}})


def _user(sid, uuid, ts, content, prompt, side=False):
    return _row(sid, type="user", uuid=uuid, parentUuid=None, timestamp=ts,
                promptId=prompt, isSidechain=side,
                message={"role": "user", "content": content})


def _history(sid, prompt_id):
    """The conversation before the switch, as the session `sid` writes it."""
    return [
        _user(sid, "h1", "2026-09-30T10:00:00.000Z", "build the thing", prompt_id),
        _assistant(sid, "h2", "2026-09-30T10:00:02.000Z", "msg_before",
                   [{"type": "tool_use", "id": "toolu_before", "name": "Bash",
                     "input": {"command": "make build"}}], 300, "tool_use"),
        _user(sid, "h3", "2026-09-30T10:00:05.000Z",
              [{"type": "tool_result", "tool_use_id": "toolu_before",
                "is_error": False, "content": "built"}], prompt_id),
        _assistant(sid, "h4", "2026-09-30T10:00:07.000Z", "msg_done",
                   [{"type": "text", "text": "It builds."}], 40),
    ]


def _after_switch(sid=NEW):
    return [
        _user(sid, "n1", "2026-09-30T10:06:00.000Z", "now test it", "p-new"),
        _assistant(sid, "n2", "2026-09-30T10:06:03.000Z", "msg_after",
                   [{"type": "text", "text": "Tests pass."}], 25),
    ]


def _write(path, rows):
    with open(str(path), "a") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


def _enable(home, paths):
    with open(config.path_rules_path(home), "w") as fh:
        json.dump({"enabled_paths": paths}, fh)


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    (h / ".claude" / "rius").mkdir(parents=True)
    _enable(str(h), ["/tmp"])
    return str(h)


@pytest.fixture
def sent(monkeypatch):
    """Every span handed to the encoder, in order."""
    out = []

    def encode(resource, span_list):
        out.extend(span_list)
        return b"x"

    monkeypatch.setattr(exporter.otlp, "encode", encode)
    monkeypatch.setattr(exporter.otlp, "export",
                        lambda ep, key, body, timeout=5.0: 200)
    return out


def _hook(home, sid, path, event):
    return exporter.run(event, {"session_id": sid, "cwd": "/tmp/proj",
                                "transcript_path": str(path),
                                "hook_event_name": event}, ENV, home)


class Conv:
    def __init__(self, tmp_path, home):
        self.home = home
        self.proj = tmp_path / "proj"
        self.proj.mkdir()
        self.old = self.proj / (OLD + ".jsonl")
        self.new = self.proj / (NEW + ".jsonl")

    def before(self, stop=True):
        _write(self.old, _history(OLD, "p-old"))
        _hook(self.home, OLD, self.old, "PostToolUse")
        if stop:
            _hook(self.home, OLD, self.old, "Stop")

    def switch(self, continued_in=True, extra_old=()):
        if extra_old:
            _write(self.old, list(extra_old))      # written, never hooked
        if continued_in:
            _write(self.old, [{"type": "continued-in", "timestamp": SWITCH_TS,
                               "sessionId": OLD, "continuedInSessionId": NEW}])
        _write(self.new, [{"type": "ai-title", "sessionId": NEW, "aiTitle": "t"},
                          {"type": "mode", "sessionId": NEW, "mode": "normal"}])
        copied = _history(NEW, "p-copy") + [dict(r, sessionId=NEW, promptId="p-copy")
                                            if r.get("promptId") else dict(r, sessionId=NEW)
                                            for r in extra_old]
        _write(self.new, copied)

    def run(self, event, sid=NEW):
        return _hook(self.home, sid, self.new if sid == NEW else self.old, event)


def _latest(spans_sent, trace_id=TRACE):
    latest = {}
    for s in spans_sent:
        if s.trace_id != trace_id:
            continue
        if s.pending and s.span_id in latest and not latest[s.span_id].pending:
            continue
        latest[s.span_id] = s
    return latest


def _llm_outputs(trace):
    return sorted(s.attributes["gen_ai.usage.output_tokens"]
                  for s in trace.values() if s.kind_oi == "LLM")


def _full(tmp_path, home):
    c = Conv(tmp_path, home)
    c.before()
    c.switch()
    c.run("SessionStart")
    _write(c.new, _after_switch())
    c.run("Stop")
    c.run("SessionEnd")
    return c


# --- Kiran's rule: one conversation, one trace ------------------------------

def test_the_whole_conversation_is_one_trace(tmp_path, home, sent):
    _full(tmp_path, home)
    assert _llm_outputs(_latest(sent)) == [25, 40, 300]
    assert not [s for s in sent if s.trace_id != TRACE]


def test_new_work_hangs_under_the_same_root_and_session(tmp_path, home, sent):
    _full(tmp_path, home)
    trace = _latest(sent)
    new_turn = [s for s in trace.values() if s.name == "turn"
                and s.attributes.get("cc.continued_from")]
    assert len(new_turn) == 1
    assert new_turn[0].parent_span_id == ROOT
    assert new_turn[0].attributes["cc.continued_from"] == OLD
    assert new_turn[0].attributes["cc.claude_session_id"] == NEW
    for s in trace.values():
        assert s.attributes["session.id"] == OLD


def test_a_genuinely_new_session_gets_its_own_trace(tmp_path, home, sent):
    """No continued-in record: a new session, sharing nothing, a new trace."""
    c = Conv(tmp_path, home)
    c.before()
    c.switch(continued_in=False)
    c.run("Stop")
    assert _llm_outputs(_latest(sent, spans.trace_id_for(NEW))) == [40, 300]


# --- invariant 7 and the copied history ------------------------------------

def test_copied_history_is_never_sent_again(tmp_path, home, sent):
    c = Conv(tmp_path, home)
    c.before()
    del sent[:]
    c.switch()
    c.run("SessionStart")
    c.run("Stop")
    copied = {spans.span_id_for(k) for k in ("msg_before", "msg_done",
                                             "toolu_before")}
    assert not [s for s in sent if s.span_id in copied]
    for s in sent:
        blob = repr(s.attributes) + (s.status_message or "")
        for secret in ("build the thing", "It builds.", "make build"):
            assert secret not in blob


def test_copied_lines_the_old_session_never_sent_are_sent_once(tmp_path, home, sent):
    """The old process wrote a reply after its last hook: it is in the copy,
    and only the new session can send it."""
    late = _assistant(OLD, "h5", "2026-09-30T10:00:09.000Z", "msg_late",
                      [{"type": "text", "text": "Also linted."}], 11)
    c = Conv(tmp_path, home)
    c.before()
    c.switch(extra_old=[late])
    c.run("Stop")
    late_id = spans.span_id_for("msg_late")
    assert len([s for s in sent if s.span_id == late_id]) == 1
    assert _latest(sent)[late_id].trace_id == TRACE


# --- invariants 3, 4 and 5: who closes what --------------------------------

def test_the_root_is_not_closed_at_the_switch(tmp_path, home, sent):
    c = Conv(tmp_path, home)
    c.before()
    c.switch()
    c.run("SessionStart")
    _write(c.new, _after_switch())
    c.run("Stop")
    assert not [s for s in sent if s.span_id == ROOT and not s.pending]


def test_the_continued_session_end_closes_the_root(tmp_path, home, sent):
    _full(tmp_path, home)
    roots = [s for s in sent if s.span_id == ROOT]
    last = roots[-1]
    assert last.pending is False
    assert last.trace_id == TRACE
    assert last.start_ns == _ns("2026-09-30T10:00:00.000Z")
    assert last.end_ns >= _ns("2026-09-30T10:06:03.000Z")
    assert not any(s.pending for s in _latest(sent).values())
    assert state.load(NEW, home).get("finalized") is True


def test_a_turn_open_at_the_switch_is_closed_by_the_continued_session(
        tmp_path, home, sent):
    c = Conv(tmp_path, home)
    c.before(stop=False)                     # the old turn is still open
    c.switch()
    c.run("SessionStart")
    c.run("SessionEnd")
    old_turn = spans.span_id_for("turn:p-old")
    trace = _latest(sent)
    assert trace[old_turn].pending is False
    assert not any(s.pending for s in trace.values())


def test_the_old_id_hands_over_and_never_closes_the_root(tmp_path, home, sent):
    """Even if the old process somehow gets a late SessionEnd."""
    c = Conv(tmp_path, home)
    c.before()
    c.switch()
    c.run("SessionStart")
    old = state.load(OLD, home)
    assert old["handed_off_to"] == NEW
    del sent[:]
    c.run("SessionEnd", sid=OLD)
    assert not [s for s in sent if s.span_id == ROOT]


def test_a_continuation_of_a_continuation_is_still_the_same_trace(
        tmp_path, home, sent):
    c = _full(tmp_path, home)
    newer = c.proj / (NEWER + ".jsonl")
    _write(c.new, [{"type": "continued-in", "timestamp": "2026-09-30T10:07:00.000Z",
                    "sessionId": NEW, "continuedInSessionId": NEWER}])
    _write(newer, [dict(r, sessionId=NEWER) for r in
                   _history(NEWER, "p2") + _after_switch(NEWER)])
    _write(newer, [_user(NEWER, "m1", "2026-09-30T10:08:00.000Z", "ship it", "p3"),
                   _assistant(NEWER, "m2", "2026-09-30T10:08:02.000Z", "msg_ship",
                              [{"type": "text", "text": "Shipped."}], 7)])
    _hook(home, NEWER, newer, "Stop")
    _hook(home, NEWER, newer, "SessionEnd")
    assert _llm_outputs(_latest(sent)) == [7, 25, 40, 300]
    assert _latest(sent)[ROOT].pending is False
    assert _latest(sent)[ROOT].end_ns >= _ns("2026-09-30T10:08:02.000Z")


# --- invariant 5 subtlety: background subagents at the switch --------------

def test_a_subagent_running_at_the_switch_keeps_landing_in_the_trace(
        tmp_path, home, sent):
    c = Conv(tmp_path, home)
    subdir = c.proj / OLD / "subagents"
    subdir.mkdir(parents=True)
    (subdir / "agent-bg1.meta.json").write_text(json.dumps({
        "agentType": "general-purpose", "toolUseId": "toolu_bg",
        "requestShape": "background", "model": "haiku"}))
    sub = subdir / "agent-bg1.jsonl"
    _write(c.old, [_user(OLD, "g1", "2026-09-30T10:00:00.000Z", "fan out", "p-old"),
                   _assistant(OLD, "g2", "2026-09-30T10:00:01.000Z", "msg_fan",
                              [{"type": "tool_use", "id": "toolu_bg", "name": "Agent",
                                "input": {"prompt": "count",
                                          "run_in_background": True}}], 9,
                              "tool_use")])
    _write(sub, [_user(OLD, "s1", "2026-09-30T10:00:01.500Z", "count", "p-old", side=True)])
    _hook(home, OLD, c.old, "PreToolUse")
    _write(c.old, [_user(OLD, "g3", "2026-09-30T10:00:02.000Z",
                         [{"type": "tool_result", "tool_use_id": "toolu_bg",
                           "is_error": False, "content": "launched"}], "p-old")])
    _hook(home, OLD, c.old, "Stop")
    # The switch, while the subagent is still working.
    _write(c.old, [{"type": "continued-in", "timestamp": SWITCH_TS,
                    "sessionId": OLD, "continuedInSessionId": NEW}])
    _write(c.new, [dict(json.loads(l), sessionId=NEW)
                   for l in open(str(c.old)).read().splitlines()
                   if json.loads(l).get("type") in ("user", "assistant")])
    c.run("SessionStart")
    _write(sub, [_assistant(OLD, "s2", "2026-09-30T10:05:30.000Z", "msg_sub",
                            [{"type": "text", "text": "42"}], 13, side=True)])
    c.run("Stop")
    c.run("SessionEnd")
    trace = _latest(sent)
    agent = trace[spans.span_id_for("subagent:agent-bg1")]
    assert agent.pending is False
    assert agent.end_ns == _ns("2026-09-30T10:05:30.000Z")
    sub_llm = [s for s in trace.values() if s.kind_oi == "LLM"
               and s.parent_span_id == agent.span_id]
    assert [s.attributes["gen_ai.usage.output_tokens"] for s in sub_llm] == [13]
    assert not any(s.pending for s in trace.values())


# --- invariants 1 and 2: the stop belongs to the conversation --------------

def test_a_stopped_conversation_stays_stopped_under_the_new_id(tmp_path, home, sent):
    c = Conv(tmp_path, home)
    c.before()
    st = state.load(OLD, home)
    st["content_stopped"] = True
    state.save(OLD, home, st)
    c.switch()
    c.run("SessionStart")
    _write(c.new, _after_switch())
    c.run("Stop")
    assert state.load(NEW, home).get("content_stopped") is True
    assert not [s for s in sent if s.span_id == spans.span_id_for("msg_after")]
    del sent[:]
    c.run("SessionEnd")
    closing = _latest(sent)
    assert closing[ROOT].pending is False
    for s in sent:
        assert "input.value" not in s.attributes
        assert "output.value" not in s.attributes


def test_a_disable_after_the_switch_still_closes_the_shared_root(tmp_path, home, sent):
    c = Conv(tmp_path, home)
    c.before()
    c.switch()
    c.run("SessionStart")
    _write(c.new, _after_switch())
    c.run("Stop")
    # hook.py's gate reads the CURRENT id's state: it must see an open trace.
    assert state.trace_is_open(state.load(NEW, home))
    _enable(home, [])                            # folder disabled mid-session
    c.run("SessionEnd")
    last = [s for s in sent if s.span_id == ROOT][-1]
    assert last.pending is False and last.trace_id == TRACE


# --- invariant 6 ------------------------------------------------------------

def test_the_old_heartbeat_is_stopped_at_the_switch(tmp_path, home, sent):
    c = Conv(tmp_path, home)
    c.before()
    c.switch()
    c.run("SessionStart")
    assert os.path.exists(os.path.join(state.state_dir(home),
                                       OLD + ".heartbeat.stop"))
    assert state.load(NEW, home)["instance_id"] != state.load(OLD, home)["instance_id"]


# --- invariant 9: the predecessor match is exact ----------------------------

def _decoy(proj, name, points_at, ts="2026-09-30T10:04:00.000Z"):
    path = proj / (name + ".jsonl")
    _write(path, [_user(name, "d-" + name[:4], "2026-09-30T09:00:00.000Z",
                        "another conversation", "pd"),
                  {"type": "continued-in", "timestamp": ts, "sessionId": name,
                   "continuedInSessionId": points_at}])
    return path


def test_a_continued_in_record_for_another_session_links_nothing(
        tmp_path, home, sent):
    c = Conv(tmp_path, home)
    c.before()
    c.switch(continued_in=False)
    _decoy(c.proj, "dddddddd-0000-0000-0000-000000000004",
           "eeeeeeee-0000-0000-0000-000000000005")
    _decoy(c.proj, "ffffffff-0000-0000-0000-000000000006",
           "99999999-0000-0000-0000-000000000009")
    # The newest decoy even names THIS id -- as the session that moved on,
    # not as the one it moved to. Only the exact field match rejects it.
    _write(c.proj / "12121212-0000-0000-0000-000000000012.jsonl", [
        {"type": "continued-in", "timestamp": "2026-09-30T10:04:30.000Z",
         "sessionId": NEW,
         "continuedInSessionId": "34343434-0000-0000-0000-000000000034"}])
    assert exporter.continuation.find_predecessor(str(c.new), NEW) is None
    c.run("Stop")
    assert "continued_from" not in state.load(NEW, home)
    assert _llm_outputs(_latest(sent, spans.trace_id_for(NEW))) == [40, 300]


def test_the_right_record_is_found_behind_newer_decoys(tmp_path, home, sent):
    c = Conv(tmp_path, home)
    c.before()
    c.switch()
    _decoy(c.proj, "dddddddd-0000-0000-0000-000000000004",
           "eeeeeeee-0000-0000-0000-000000000005")
    old_id, old_path, switch_ns = exporter.continuation.find_predecessor(
        str(c.new), NEW)
    assert (old_id, old_path, switch_ns) == (OLD, str(c.old), _ns(SWITCH_TS))


# --- invariant 8: a continuation into a disabled folder ---------------------

def test_a_continuation_into_a_disabled_folder_still_closes_the_root(
        tmp_path, home, sent, monkeypatch):
    from tests.test_hook import _run_in_process
    c = Conv(tmp_path, home)
    c.before()
    c.switch()
    _enable(home, [])                            # disabled before the switch lands
    payload = {"session_id": NEW, "cwd": "/tmp/proj",
               "transcript_path": str(c.new), "hook_event_name": "SessionStart"}
    calls = _run_in_process(monkeypatch, "SessionStart", payload,
                            dict(ENV, HOME=home, USERPROFILE=home), home)
    assert any(a.endswith("exporter.py") for call in calls for a in call["argv"])
    assert not any(a.endswith("heartbeat.py") for call in calls for a in call["argv"])
    c.run("SessionStart")
    c.run("SessionEnd")
    last = [s for s in sent if s.span_id == ROOT][-1]
    assert last.pending is False and last.trace_id == TRACE
    assert not any(s.pending for s in _latest(sent).values())


# --- invariant 10: one pinger per root ---------------------------------------

def test_the_old_pinger_is_stopped_before_the_new_one_starts(
        tmp_path, home, sent, monkeypatch):
    from tests.test_hook import _run_in_process
    c = Conv(tmp_path, home)
    c.before()
    c.switch()
    stop = os.path.join(state.state_dir(home), OLD + ".heartbeat.stop")
    seen = []
    real_exists = os.path.exists

    import hook as hook_mod

    class Recorder:
        def __init__(self, argv, **kw):
            if any(isinstance(a, str) and a.endswith("heartbeat.py") for a in argv):
                seen.append(real_exists(stop))
            if not any(isinstance(a, str) and a.endswith(("exporter.py", "heartbeat.py"))
                       for a in argv):
                raise OSError("no ps")

    payload = {"session_id": NEW, "cwd": "/tmp/proj",
               "transcript_path": str(c.new), "hook_event_name": "SessionStart"}
    monkeypatch.setattr(os, "environ", dict(ENV, HOME=home, USERPROFILE=home))
    monkeypatch.setattr(hook_mod.subprocess, "Popen", Recorder)
    import io, sys
    monkeypatch.setattr(sys, "argv", ["hook.py", "SessionStart"])
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    hook_mod.main()
    assert seen == [True], "the new pinger started before the old one was told to stop"


def test_the_exporter_alone_finds_a_continuation_into_a_disabled_folder(
        tmp_path, home, sent):
    """Without hook.py's SessionStart link (its lock was busy, say), the
    exporter must still find the conversation before it takes the stopped
    path, or the stopped path sees no open trace and closes nothing."""
    c = Conv(tmp_path, home)
    c.before()
    c.switch()
    _enable(home, [])
    c.run("SessionStart")
    c.run("SessionEnd")
    last = [s for s in sent if s.span_id == ROOT][-1]
    assert last.pending is False and last.trace_id == TRACE


def test_a_handed_off_state_never_closes_the_root():
    st = state.new_state()
    st.update(root_started=True, root_start_ns=1, handed_off_to=NEW)
    ctx = spans.Ctx(session_id=OLD, cwd="/tmp", git_branch="", cc_version="",
                    service_name="claude-code", capture_content=False,
                    max_attr_bytes=1024)
    assert [s for s in spans.finalize_session(st, ctx, 10) if s.span_id == ROOT] == []


def test_the_old_id_sends_nothing_after_the_switch(tmp_path, home, sent):
    """A late hook of the old process must not send the lines the new id now
    owns: each is sent once."""
    late = _assistant(OLD, "h5", "2026-09-30T10:00:09.000Z", "msg_late",
                      [{"type": "text", "text": "Also linted."}], 11)
    c = Conv(tmp_path, home)
    c.before()
    c.switch(extra_old=[late])
    c.run("Stop")
    c.run("Stop", sid=OLD)
    c.run("SessionEnd", sid=OLD)
    assert len([s for s in sent if s.span_id == spans.span_id_for("msg_late")]) == 1
