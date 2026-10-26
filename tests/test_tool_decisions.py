"""With the experimental opt-in, a tool span carries the decision Claude Code
made on the call before running it: a `gen_ai.tool.call.decision` span
event, in the shape an open OTel GenAI proposal gives it (outcome `allow`,
`deny` or `require_approval`, with the call's id and tool name).

The decision comes from the fields Claude Code writes on the entry holding
the call's result, never from the result's text: `permissionDecision`
(2.1.296 and later) and, before that, `toolDenialKind` on a denial. The
fixture keeps the shape of real transcripts, with the content cut.
"""
import json

import pytest

import exporter
from rius_cc import config, spans, state, subagents, transcript
from rius_cc import _semconv_generated as semconv
from tests import signed_in

FIXTURE = "tool_decisions.jsonl"
SESSION = "99999999-0000-4000-8000-000000000535"
OPT_IN = "RIUS_CLAUDE_EXPERIMENTAL_TOOL_DECISIONS"


def _tools(fixtures_dir, decisions=True, capture=False):
    path = str(fixtures_dir / FIXTURE)
    entries, _ = transcript.read_from(path, 0)
    ctx = spans.Ctx(session_id=SESSION, cwd="/tmp/proj", git_branch="main",
                    cc_version="2.1.303", service_name="claude-code",
                    capture_content=capture, max_attr_bytes=32768,
                    tool_decisions=decisions)
    st = {"offset": 0, "open_tools": {}, "open_turns": {},
          "root_started": False, "root_start_ns": 0, "last_ns": 0,
          "open_task_spans": []}
    out = spans.build(entries, st, ctx, source_path=path)
    return {s.attributes[semconv.GEN_AI_TOOL_CALL_ID]: s for s in out
            if s.kind_oi == "TOOL" and not s.pending}


def _decisions(span):
    return [(at, attrs[spans.TOOL_CALL_DECISION_OUTCOME])
            for at, name, attrs in span.events
            if name == spans.TOOL_CALL_DECISION_EVENT]


def _outcomes(span):
    return [outcome for _, outcome in _decisions(span)]


def test_each_recorded_decision_becomes_an_event(fixtures_dir):
    tools = _tools(fixtures_dir)
    assert {k: _outcomes(s) for k, s in tools.items()} == {
        "toolu_allow": ["allow"],
        "toolu_classifier": ["deny"],
        "toolu_safety": ["deny"],
        "toolu_approved": ["require_approval", "allow"],
        "toolu_rejected": ["require_approval", "deny"],
        "toolu_old_reject": ["require_approval", "deny"],
        "toolu_unrecorded": [],
    }


def test_the_event_names_the_call_and_its_tool(fixtures_dir):
    span = _tools(fixtures_dir)["toolu_rejected"]
    for _, name, attrs in span.events:
        if name == spans.TOOL_CALL_DECISION_EVENT:
            assert attrs[semconv.GEN_AI_TOOL_CALL_ID] == "toolu_rejected"
            assert attrs[semconv.GEN_AI_TOOL_NAME] == "Edit"
            assert set(attrs) == {semconv.GEN_AI_TOOL_CALL_ID,
                                  semconv.GEN_AI_TOOL_NAME,
                                  spans.TOOL_CALL_DECISION_OUTCOME}


def test_a_denial_is_dated_when_it_came_back_and_the_rest_at_the_proposal(
        fixtures_dir):
    """Claude Code does not record when the user answered. A denial is known
    when its result is written; an approval only that it came before the run,
    so it is put 1 ns after the ask, for a backend that orders by time."""
    tools = _tools(fixtures_dir)
    rejected = tools["toolu_rejected"]
    assert _decisions(rejected) == [(rejected.start_ns, "require_approval"),
                                    (rejected.end_ns, "deny")]
    approved = tools["toolu_approved"]
    assert _decisions(approved) == [(approved.start_ns, "require_approval"),
                                    (approved.start_ns + 1, "allow")]


def test_an_outcome_never_shares_the_asks_time():
    """A denial whose result is stamped in the same millisecond as the call
    still comes after the ask."""
    entry = transcript.parse_line(json.dumps({
        "type": "user", "uuid": "r1", "timestamp": "2026-10-23T12:20:00.000Z",
        "permissionDecision": {"decision": "reject", "source": "user_reject"},
        "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "x"}]}}))
    tool = {"tool_name": "Edit", "start_ns": entry.timestamp_ns}
    assert [(at, attrs[spans.TOOL_CALL_DECISION_OUTCOME]) for at, _, attrs
            in spans._decision_events(entry, "t1", tool)] == [
        (entry.timestamp_ns, "require_approval"),
        (entry.timestamp_ns + 1, "deny")]


def test_a_denied_call_still_reads_as_a_failed_tool(fixtures_dir):
    span = _tools(fixtures_dir)["toolu_classifier"]
    assert span.status_code == "ERROR"
    assert [name for _, name, _ in span.events] == [
        spans.TOOL_CALL_DECISION_EVENT, "exception"]


def test_no_events_without_the_opt_in(fixtures_dir):
    tools = _tools(fixtures_dir, decisions=False)
    assert not any(name == spans.TOOL_CALL_DECISION_EVENT
                   for s in tools.values() for _, name, _ in s.events)


def test_the_events_carry_no_content_with_capture_off(fixtures_dir):
    blob = json.dumps([s.events for s in _tools(fixtures_dir).values()])
    assert "auto mode classifier" not in blob
    assert "git push" not in blob


@pytest.mark.parametrize("raw,decision", [
    ({"permissionDecision": {"decision": "accept", "source": "config",
                             "reasonType": "rule", "ruleSource": "localSettings"}},
     ("allow", False)),
    ({"permissionDecision": {"decision": "accept", "source": "user_permanent"}},
     ("allow", True)),
    ({"permissionDecision": {"decision": "reject", "source": "config",
                             "reasonType": "classifier"},
      "toolDenialKind": "automode-unavailable"}, ("deny", False)),
    ({"toolDenialKind": "automode-blocked"}, ("deny", False)),
    ({"permissionDecision": {"decision": "ask"}}, (None, False)),
    ({"permissionDecision": "accept"}, (None, False)),
    ({"toolDenialKind": ""}, (None, False)),
    ({}, (None, False)),
])
def test_what_a_result_entry_records(raw, decision):
    assert spans.tool_decision(raw) == decision


def test_an_entry_with_several_results_gives_none_its_decision(tmp_path):
    """Claude Code writes no decision on such an entry; one that did could
    not say which call it was about."""
    lines = [
        {"type": "user", "uuid": "u1", "timestamp": "2026-10-23T12:20:00.000Z",
         "sessionId": SESSION, "promptId": "p1",
         "message": {"role": "user", "content": [{"type": "text", "text": "go"}]}},
        {"type": "assistant", "uuid": "a1", "timestamp": "2026-10-23T12:20:01.000Z",
         "sessionId": SESSION, "message": {"id": "m1", "role": "assistant", "content": [
             {"type": "tool_use", "id": "t1", "name": "Read", "input": {}},
             {"type": "tool_use", "id": "t2", "name": "Read", "input": {}}]}},
        {"type": "user", "uuid": "u2", "timestamp": "2026-10-23T12:20:02.000Z",
         "sessionId": SESSION, "promptId": "p1",
         "permissionDecision": {"decision": "accept", "source": "config"},
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "t1", "content": "x"},
             {"type": "tool_result", "tool_use_id": "t2", "content": "y"}]}},
    ]
    path = tmp_path / "t.jsonl"
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))
    entries, _ = transcript.read_from(str(path), 0)
    ctx = spans.Ctx(session_id=SESSION, cwd="/tmp", git_branch="", cc_version="",
                    service_name="claude-code", capture_content=False,
                    max_attr_bytes=32768, tool_decisions=True)
    st = {"offset": 0, "open_tools": {}, "open_turns": {}, "root_started": False,
          "root_start_ns": 0, "last_ns": 0, "open_task_spans": []}
    tools = [s for s in spans.build(entries, st, ctx) if s.kind_oi == "TOOL"
             and not s.pending]
    assert len(tools) == 2 and all(not s.events for s in tools)


SUB_SID = "7a7a7a7a-0000-4000-8000-000000000072"


def test_a_subagents_calls_carry_their_decisions(fixtures_dir):
    """A subagent's results are in its own file, and Claude Code writes the
    decision on them as it does in the main transcript."""
    path = str(fixtures_dir / "skill_subagent" / (SUB_SID + ".jsonl"))
    st = state.new_state()
    ctx = spans.Ctx(session_id=SUB_SID, cwd="/tmp/proj", git_branch="main",
                    cc_version="2.1.303", service_name="claude-code",
                    capture_content=False, max_attr_bytes=32768,
                    tool_decisions=True)
    entries, _ = transcript.read_from(path, 0)
    out = spans.build(entries, st, ctx, source_path=path)
    out += subagents.expand(st, ctx, subagents.dir_for(path, SUB_SID))
    tools = {s.attributes[semconv.GEN_AI_TOOL_CALL_ID]: _outcomes(s)
             for s in out if s.kind_oi == "TOOL" and not s.pending}
    assert tools == {"toolu_agent": [],
                     "toolu_sub_skill": ["allow"],
                     "toolu_sub_old": [],
                     "toolu_sub_rejected": ["require_approval", "deny"]}


def test_an_adopted_subagent_keeps_the_opt_in(tmp_path, monkeypatch):
    """The subagents of a session id this one took over are built with a
    context of their own; it has to carry the opt-in, or their events are
    lost without anything failing."""
    home = str(tmp_path)
    old_id = "7a7a7a7a-0000-4000-8000-0000000000a1"
    old = state.new_state()
    old["sub_links"] = {"toolu_agent": {"span_id": "0" * 16}}
    state.save(old_id, home, old)
    seen = []
    monkeypatch.setattr(subagents, "expand",
                        lambda st, ctx, subdir: seen.append(ctx) or [])
    ctx = spans.Ctx(session_id=SESSION, cwd="/tmp/proj", git_branch="main",
                    cc_version="2.1.303", service_name="claude-code",
                    capture_content=False, max_attr_bytes=32768,
                    tool_decisions=True)
    st = {"adopted": [{"session_id": old_id, "transcript_path": ""}]}
    exporter._adopted_subagents(st, ctx, None, home, "PostToolUse", 0, False)
    assert [c.tool_decisions for c in seen] == [True]


@pytest.mark.parametrize("value,on", [
    ("true", True), ("1", True), ("false", False), ("yes", False), (None, False)])
def test_the_opt_in_is_read_from_the_environment(tmp_path, value, on):
    env = {} if value is None else {OPT_IN: value}
    cfg = config.resolve(SESSION, "/tmp/proj", env, str(tmp_path))
    assert cfg.tool_decisions is on


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    (h / ".claude" / "rius").mkdir(parents=True)
    signed_in.sign_in(str(h), email="")
    with open(config.path_rules_path(str(h)), "w") as fh:
        json.dump({"enabled_paths": ["/tmp"]}, fh)
    return str(h)


def _export(home, fixtures_dir, monkeypatch, env):
    # Here, not at the top: it skips everything that imports it on Python
    # 3.9, which has no opentelemetry-proto, and the rest runs there.
    from tests import otlp_json
    sent = []
    monkeypatch.setattr(exporter.otlp, "export",
                        lambda ep, key, body, timeout=5.0: sent.append(body) or 200)
    for event in ("SessionStart", "PostToolUse", "Stop", "SessionEnd"):
        payload = {"session_id": SESSION,
                   "transcript_path": str(fixtures_dir / FIXTURE),
                   "cwd": "/tmp/proj", "hook_event_name": event}
        exporter.run(event, payload, env, home, "decision-instance")
    return [span for body in sent
            for rs in otlp_json.to_request(body).resource_spans
            for ss in rs.scope_spans for span in ss.spans]


def test_the_exporter_sends_the_events_as_otlp_span_events(home, fixtures_dir,
                                                           monkeypatch):
    sent = _export(home, fixtures_dir, monkeypatch, {OPT_IN: "true"})
    rejected = [span for span in sent if span.name == "Edit"
                and span.end_time_unix_nano > span.start_time_unix_nano]
    assert len(rejected) == 1
    events = [{kv.key: kv.value.string_value for kv in e.attributes}
              for e in rejected[0].events if e.name == spans.TOOL_CALL_DECISION_EVENT]
    assert events == [
        {"gen_ai.tool.call.decision.outcome": "require_approval",
         "gen_ai.tool.call.id": "toolu_rejected", "gen_ai.tool.name": "Edit"},
        {"gen_ai.tool.call.decision.outcome": "deny",
         "gen_ai.tool.call.id": "toolu_rejected", "gen_ai.tool.name": "Edit"},
    ]


def test_the_exporter_sends_none_by_default(home, fixtures_dir, monkeypatch):
    sent = _export(home, fixtures_dir, monkeypatch, {})
    assert sent
    assert not any(e.name == spans.TOOL_CALL_DECISION_EVENT
                   for span in sent for e in span.events)
