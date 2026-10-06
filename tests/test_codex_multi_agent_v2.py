"""Codex's opt-in `multi_agent_v2` (`[features] multi_agent_v2 = true`), from
real codex-cli 0.144.1 runs against a mock model.

What v2 does differently from v1, as recorded:

- `spawn_agent` is a direct call in the `collaboration` namespace (never
  inside `exec`), and its output names the new agent by path,
  `{"task_name":"/root/sub"}`, not by thread id.
- The parent's rollout says which thread it started, and from which call:
  `sub_agent_activity {event_id: <call id>, agent_thread_id, kind: started}`.
- The child's rollout is as in v1 (`source.subagent.thread_spawn`), and
  with the default `fork_turns` opens with a copy of its parent's history.

fixtures/codex/multi_agent_v2/simple: a parent and one child
(`fork_turns: none`). nested: a chain of three children, each spawned by the
one before (the fourth spawn failed: thread limit). Prompts and long
instructions are trimmed; hooks.json is the hook sequence of each run.
"""
import json
import pathlib
import shutil

from rius_cc import codex_rollout, codex_spans, spans

from tests.test_codex_export import ENV, codex_home, exporter, sent  # noqa: F401

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "codex" / "multi_agent_v2"
SPAWN_NAME = "collaboration__spawn_agent"
SIMPLE_ROOT = "01a10ca2-6834-7cb2-a8d8-00d3ccf18e8f"
SIMPLE_CHILD = "01a10ca2-6ad9-7940-a25e-54d048e27a67"
NESTED_ROOT = "01a10ca2-be8e-7af2-be26-87e190f17d1b"
NESTED_CHAIN = ["01a10ca2-c155-7ab2-adb8-1e5fd4c07875",
                "01a10ca2-c1a2-7d80-89c3-31fc40d121df",
                "01a10ca2-c1e9-73b3-aa24-491058720efa"]
# The spawn_agent call of each rollout of the chain, root first.
NESTED_CALLS = ["call_t3_0", "call_t3_1", "call_t3_2"]
# Every rollout's own final total_token_usage.input_tokens.
SIMPLE_INPUT_TOKENS = 8000 + 2000
NESTED_INPUT_TOKENS = 17000 + 20000 + 23000 + 18000


def _rollouts(tmp_path, name):
    folder = tmp_path / "sessions"
    shutil.copytree(str(FIXTURES / name), str(folder))
    return {p.stem[-36:]: p for p in folder.glob("rollout-*.jsonl")}


def _hooks(name):
    return json.loads((FIXTURES / name / "hooks.json").read_text())["hooks"]


def _fire(home, session_id, rollouts, hook):
    payload = {"session_id": session_id, "cwd": "/tmp/proj",
               "hook_event_name": hook["event"],
               "transcript_path": str(rollouts[hook["transcript"]])}
    for key in ("agent_id", "agent_type", "tool_name", "tool_response"):
        if hook[key]:
            payload[key] = hook[key]
    if hook["agent_transcript"]:
        payload["agent_transcript_path"] = str(rollouts[hook["agent_transcript"]])
    return exporter.run(hook["event"], payload, ENV, str(home))


def _latest(batches):
    """The last row sent per span id, pending or not."""
    return {s.span_id: s for _, out in batches for s in out}


def _finished_rows(batches):
    return [s.span_id for _, out in batches for s in out if not s.pending]


def _input_tokens(rows):
    return sum(s.attributes.get("gen_ai.usage.input_tokens") or 0
               for s in rows.values() if s.kind_oi == "LLM")


def _root_of(rows, agent_id):
    return rows.get(spans.span_id_for("subagent:" + agent_id))


def test_a_v2_spawn_is_a_collaboration_call_named_by_path():
    records, _ = codex_rollout.read_from(
        str(next((FIXTURES / "simple").glob("rollout-*-%s.jsonl" % SIMPLE_ROOT))), 0)
    spawn = next(r for r in records if r.kind == codex_rollout.TOOL_CALL
                 and r.get("name") == SPAWN_NAME)
    output = next(r for r in records if r.kind == codex_rollout.TOOL_OUTPUT
                  and r.get("call_id") == spawn.get("call_id"))
    assert json.loads(output.get("output")) == {"task_name": "/root/sub"}


def test_the_parents_rollout_alone_places_a_v2_child_under_its_spawn_call(
        codex_home, tmp_path, sent):
    """No hook of the child's own, no SubagentStop: the parent's
    `sub_agent_activity` names the thread and the call."""
    rollouts = _rollouts(tmp_path, "simple")
    _fire(tmp_path, SIMPLE_ROOT, rollouts, {
        "event": "Stop", "agent_id": "", "agent_type": "", "tool_name": "",
        "tool_response": "", "transcript": SIMPLE_ROOT, "agent_transcript": ""})
    rows = _latest(sent)
    child = _root_of(rows, SIMPLE_CHILD)
    assert child is not None, "the v2 subagent was dropped"
    spawn = rows[child.parent_span_id]
    assert (spawn.name, spawn.span_id) == (SPAWN_NAME, spans.span_id_for("call_t3_0"))
    assert _input_tokens(rows) == SIMPLE_INPUT_TOKENS


def test_a_v2_run_as_codex_fired_its_hooks_keeps_its_subagent(
        codex_home, tmp_path, sent):
    rollouts = _rollouts(tmp_path, "simple")
    for hook in _hooks("simple"):
        _fire(tmp_path, SIMPLE_ROOT, rollouts, hook)
    rows = _latest(sent)
    child = _root_of(rows, SIMPLE_CHILD)
    assert child.parent_span_id == spans.span_id_for("call_t3_0")
    assert child.name == "default"
    assert _input_tokens(rows) == SIMPLE_INPUT_TOKENS
    finished = _finished_rows(sent)
    assert len(finished) == len(set(finished))


def test_a_v2_chain_of_subagents_hangs_each_under_the_call_that_spawned_it(
        codex_home, tmp_path, sent):
    rollouts = _rollouts(tmp_path, "nested")
    for hook in _hooks("nested"):
        _fire(tmp_path, NESTED_ROOT, rollouts, hook)
    rows = _latest(sent)
    for agent_id, call in zip(NESTED_CHAIN, NESTED_CALLS):
        root = _root_of(rows, agent_id)
        assert root is not None, "%s was dropped" % agent_id
        assert root.parent_span_id == spans.span_id_for(call), agent_id
    # Forked children begin with their ancestors' history: not counted twice.
    assert _input_tokens(rows) == NESTED_INPUT_TOKENS
    finished = _finished_rows(sent)
    assert len(finished) == len(set(finished))


def test_a_failed_v2_spawn_makes_no_subagent(codex_home, tmp_path, sent):
    """The fourth spawn of the chain hit the thread limit: its output is an
    error line and there is no `sub_agent_activity` for it."""
    rollouts = _rollouts(tmp_path, "nested")
    _fire(tmp_path, NESTED_ROOT, rollouts, {
        "event": "Stop", "agent_id": "", "agent_type": "", "tool_name": "",
        "tool_response": "", "transcript": NESTED_ROOT, "agent_transcript": ""})
    roots = [s for s in _latest(sent).values()
             if s.kind_oi == "AGENT" and s.parent_span_id is not None]
    assert len(roots) == len(NESTED_CHAIN)


def _activity(kind):
    return json.dumps({
        "timestamp": "2026-10-05T15:15:33.248Z", "type": "event_msg",
        "payload": {"type": "sub_agent_activity", "event_id": "call_t3_0",
                    "occurred_at_ms": 1791213333248, "kind": kind,
                    "agent_thread_id": SIMPLE_CHILD, "agent_path": "/root/sub"}})


def test_a_started_activity_names_the_agent_and_the_call():
    record = codex_rollout.parse_line(_activity("started"))
    assert record.kind == codex_rollout.SUBAGENT_STARTED
    assert (record.get("call_id"), record.get("agent_id")) == ("call_t3_0",
                                                               SIMPLE_CHILD)


def test_any_other_activity_is_not_read():
    assert codex_rollout.parse_line(_activity("finished")) is None


def test_a_started_agent_is_only_placed_under_a_call_still_open():
    """A forked child's history holds its ancestors' `sub_agent_activity`
    lines; their calls are not open in the child's own state."""
    ctx = spans.Ctx(session_id="s", cwd="/tmp", git_branch="", cc_version="",
                    service_name="codex", capture_content=False,
                    max_attr_bytes=32768)
    state = codex_spans.new_state()
    codex_spans.build([
        codex_rollout.Record(codex_rollout.TURN_START, 1, {"turn_id": "t1"}),
        codex_rollout.Record(codex_rollout.SUBAGENT_STARTED, 2, {
            "call_id": "call_gone", "agent_id": "01a10ca2-c155-7ab2-adb8-1e5fd4c07875"}),
    ], state, ctx)
    assert state["spawned"] == {}
