"""A session state saved by 0.6.0, picked up by this builder.

0.6.0 kept the exec calls in `tool_starts` and, in plaintext, the text of the
open turn and the input and MCP error of each open tool. A session that
spans the upgrade must lose none of its subagents or errors, and must not
keep the plaintext past the first hook of the new version.
"""
import json

import pytest

from rius_cc import agent, codex_session, codex_spans, config, spans, state

from tests import signed_in
from tests.test_codex_code_mode import (CHILD, PARENT, _rollouts, exporter, sent)  # noqa: F401
from tests.test_codex_export import codex_home  # noqa: F401

LEGACY = "LEGACY PLAINTEXT THAT 0.6.0 KEPT"
OFF = {"RIUS_CAPTURE_CONTENT": "false"}


def _home(tmp_path, name):
    root = tmp_path / name
    root.mkdir()
    with agent.using(agent.CODEX):
        signed_in.sign_in(str(root))
        config.write_path_rules(str(root), {"enabled_paths": ["/tmp"]})
    return root


def _fire(root, rollouts, event, rollout, **extra):
    payload = {"session_id": PARENT, "cwd": "/tmp/proj", "hook_event_name": event,
               "transcript_path": str(rollouts[rollout])}
    payload.update(extra)
    with agent.using(agent.CODEX):
        exporter.run(event, payload, OFF, str(root))


def _as_0_6_0(root):
    """The session's saved state as 0.6.0 left it."""
    saved = state.load(PARENT, str(root))
    for each in [saved] + [sub["state"] for sub in saved["codex_subs"].values()
                           if sub.get("state")]:
        each["tool_starts"] = [[call["start_ns"], call["span_id"], "exec"]
                               for call in each.pop("execs")]
        if each["turn"]:
            for key in ("text_at", "reply_at"):
                each["turn"].pop(key, None)
            each["turn"].update(text=LEGACY, reply=LEGACY)
        for tool in each["open_tools"].values():
            for key in ("input_at", "secret_file", "mcp_failed", "error_at",
                        "span_name"):
                tool.pop(key, None)
            tool.update(input_json=LEGACY, mcp_error=None)
    state.save(PARENT, str(root), saved)


def _the_run(tmp_path, name, sent, cut, upgrade):
    """The code-mode fixture's hooks, the parent rollout cut after `cut`
    lines for the first of them; 0.6.0 is what saved the state before the
    rest if `upgrade`. The last row of each span the run sent."""
    root = _home(tmp_path, name)
    rollouts = _rollouts(tmp_path / name)
    parent = rollouts[PARENT]
    whole = parent.read_bytes()
    parent.write_bytes(b"".join(whole.splitlines(keepends=True)[:cut]))
    del sent[:]
    _fire(root, rollouts, "UserPromptSubmit", PARENT)
    if upgrade:
        _as_0_6_0(root)
    parent.write_bytes(whole)
    _fire(root, rollouts, "PostToolUse", PARENT, tool_name="spawn_agent",
          tool_response=json.dumps({"agent_id": CHILD}))
    _fire(root, rollouts, "PostToolUse", CHILD, agent_id=CHILD,
          agent_type="default", tool_name="Bash")
    _fire(root, rollouts, "SubagentStop", PARENT, agent_id=CHILD,
          agent_type="default", agent_transcript_path=str(rollouts[CHILD]))
    _fire(root, rollouts, "Stop", PARENT)
    return root, {s.span_id: s for _, out in sent for s in out}


def _shape(rows):
    return {span_id: (s.name, s.parent_span_id, s.start_ns, s.end_ns,
                      s.status_code, s.pending, json.dumps(s.attributes, sort_keys=True))
            for span_id, s in rows.items()}


@pytest.mark.parametrize("cut", range(5, 17))
def test_a_session_upgraded_mid_run_sends_what_an_unbroken_one_sends(
        codex_home, tmp_path, sent, cut):
    """The spawning exec call has begun when 0.6.0 saves the state (cuts 11
    and 12) or is over, and the child's first hook comes after the upgrade."""
    _, control = _the_run(tmp_path, "control", sent, cut, upgrade=False)
    root, upgraded = _the_run(tmp_path, "upgraded", sent, cut, upgrade=True)
    assert spans.span_id_for("subagent:" + CHILD) in upgraded
    assert _shape(upgraded) == _shape(control)
    saved = json.dumps(state.load(PARENT, str(root)))
    assert LEGACY not in saved and "tool_starts" not in saved


# --- a hand-built 0.6.0 state ----------------------------------------------


def _legacy_state():
    turn_span = spans.span_id_for("turn:t1")
    return {
        "root_started": True, "root_start_ns": 1, "finalized": False,
        "session": {}, "model": "m", "spawned": {}, "thread_id": PARENT,
        "turn": {"turn_id": "t1", "span_id": turn_span, "start_ns": 2,
                 "text": LEGACY, "reply": LEGACY, "llm_index": 0, "llm_start_ns": 2},
        "open_tools": {
            "c1": {"span_id": spans.span_id_for("c1"), "parent_span_id": turn_span,
                   "start_ns": 3, "tool_name": "mcp__db__query",
                   "input_json": LEGACY, "mcp_error": "auth failed"},
            "c2": {"span_id": spans.span_id_for("c2"), "parent_span_id": turn_span,
                   "start_ns": 4, "tool_name": "exec_command",
                   "input_json": LEGACY, "mcp_error": None}},
        "tool_starts": [[3, spans.span_id_for("c1"), "mcp__db__query"],
                        [5, spans.span_id_for("c3"), "exec"],
                        [4, spans.span_id_for("c2"), "exec"]],
    }


def test_the_plaintext_of_a_0_6_0_state_is_gone_once_it_is_loaded():
    saved = _legacy_state()
    saved["codex_subs"] = {CHILD: {"path": "", "agent_type": "", "offset": 0,
                                   "state": _legacy_state()}}
    loaded = codex_session.load(saved)
    assert LEGACY not in json.dumps(loaded)
    assert "tool_starts" not in json.dumps(loaded)
    assert "tool_starts" not in loaded["codex_subs"][CHILD]["state"]


def test_the_exec_calls_of_a_0_6_0_state_are_kept_open_ones_running():
    loaded = codex_session.load(_legacy_state())
    by_id = {call["span_id"]: call for call in loaded["execs"]}
    assert set(by_id) == {spans.span_id_for("c2"), spans.span_id_for("c3")}
    assert by_id[spans.span_id_for("c2")]["end_ns"] == 0          # still open
    assert by_id[spans.span_id_for("c3")]["end_ns"] == 5          # over
    assert codex_spans.spawning_tool(loaded, 6) == spans.span_id_for("c2")


@pytest.mark.parametrize("capture", [False, True])
def test_an_mcp_call_that_had_failed_before_the_upgrade_still_ends_as_an_error(capture):
    loaded = codex_session.load(_legacy_state())
    ctx = spans.Ctx(session_id=PARENT, cwd="/tmp", git_branch="", cc_version="",
                    service_name="codex", capture_content=capture,
                    max_attr_bytes=32768)
    from rius_cc import codex_rollout
    out = codex_spans.build([codex_rollout.Record(codex_rollout.TOOL_OUTPUT, 9, {
        "call_id": "c1", "output": "[]"})], loaded, ctx)
    tool = [s for s in out if s.kind_oi == "TOOL" and not s.pending][0]
    assert tool.status_code == "ERROR"
    assert tool.attributes["error.type"] == "mcp__db__query.tool_error"
    assert LEGACY not in json.dumps([tool.attributes, tool.events])


def test_what_a_call_gave_before_the_upgrade_is_withheld_not_guessed():
    """Whether the call read a secret file was settled in the state 0.6.0 no
    longer has; its output is not sent."""
    loaded = codex_session.load(_legacy_state())
    ctx = spans.Ctx(session_id=PARENT, cwd="/tmp", git_branch="", cc_version="",
                    service_name="codex", capture_content=True, max_attr_bytes=32768)
    from rius_cc import codex_rollout
    out = codex_spans.build([codex_rollout.Record(codex_rollout.TOOL_OUTPUT, 9, {
        "call_id": "c2", "output": "TOKEN=abc123"})], loaded, ctx)
    tool = [s for s in out if s.kind_oi == "TOOL" and not s.pending][0]
    assert tool.attributes["output.value"] == codex_spans.OUTPUT_NOT_CHECKED
