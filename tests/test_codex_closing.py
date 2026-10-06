"""A session that dies with a spawn call open, and the subagents it leaves.

While a `spawn_agent` call is open, the output (or multi_agent_v2's
`sub_agent_activity`) will name the child exactly, so a child seen before
that waits. If the session dies first nothing more will come, and the child
must still be placed and exported when its trace is closed: the stale sweep
runs nothing but `codex_session.finalize`.
"""
import json

import pytest

from rius_cc import agent, codex_session, config, spans, state

from tests.test_codex_code_mode import (  # noqa: F401
    NO_TOOL_SCRIPT, PARENT, T0, _event, _exec_call, _exec_done, _item,
    _latest, _line, _rollout, _uuid7_at, exporter)
from tests.test_codex_export import (ENV, HOUR_NS, _sweep,  # noqa: F401
                                     codex_home, sent)

def _state(home):
    return state.load(PARENT, str(home))


SPAWN = {"type": "function_call", "name": "spawn_agent", "call_id": "call_spawn",
         "namespace": "multi_agent_v1", "arguments": "{}"}


def _meta(agent_id, parent=None, ms=0):
    payload = {"id": agent_id, "cwd": "/tmp/proj", "cli_version": "0.144.1"}
    if parent:
        payload["source"] = {"subagent": {"thread_spawn": {"parent_thread_id": parent}}}
    return _line(T0 + ms, "session_meta", payload)


def _child(folder, parent, ms=30):
    child = _uuid7_at(T0 + ms, "c" * 19)
    path = _rollout(folder / ("rollout-c-%s.jsonl" % child), [
        _meta(child, parent, ms + 1),
        _event(T0 + ms + 10, {"type": "task_started", "turn_id": "t-child"}),
        _event(T0 + ms + 20, {"type": "user_message", "message": "go"}),
        _event(T0 + ms + 300, {"type": "task_complete", "turn_id": "t-child",
                               "last_agent_message": ""})])
    return child, path


def _dying_parent(folder, with_exec=True):
    """A parent that began a turn, ran one finished exec and began a spawn_agent
    call that never got its output: the process was killed."""
    folder.mkdir()
    lines = [_meta(PARENT, ms=-100), _event(T0, {"type": "task_started", "turn_id": "t1"})]
    if with_exec:
        lines += [_exec_call(T0 + 1, "call_exec", NO_TOOL_SCRIPT),
                  _exec_done(T0 + 5, "call_exec")]
    lines.append(_item(T0 + 10, dict(SPAWN)))
    return _rollout(folder / ("rollout-p-%s.jsonl" % PARENT), lines)


def _hooks(tmp_path, parent, child, child_path):
    for event, extra in (
            ("UserPromptSubmit", {}),
            ("SubagentStop", {"agent_id": child, "agent_type": "default",
                              "agent_transcript_path": str(child_path)}),
            ("Stop", {})):
        payload = {"session_id": PARENT, "cwd": "/tmp/proj", "hook_event_name": event,
                   "transcript_path": str(parent)}
        payload.update(extra)
        exporter.run(event, payload, ENV, str(tmp_path))


def _after_a_sweep(codex_home, tmp_path, sent, with_exec=True):
    folder = tmp_path / "sessions"
    parent = _dying_parent(folder, with_exec)
    child, child_path = _child(folder, PARENT)
    _hooks(tmp_path, parent, child, child_path)
    root = spans.span_id_for("subagent:" + child)
    assert root not in _latest(sent), "the child waits while the spawn call is open"
    last_ns = _state(tmp_path)["last_ns"]
    del sent[:]
    _sweep(tmp_path, last_ns + 2 * HOUR_NS)
    return child, {s.span_id: s for _, out in sent for s in out}


def test_a_child_waiting_on_an_open_spawn_call_is_placed_when_the_trace_closes(
        codex_home, tmp_path, sent):
    child, rows = _after_a_sweep(codex_home, tmp_path, sent)
    root = rows[spans.span_id_for("subagent:" + child)]
    assert not root.pending
    assert root.parent_span_id == spans.span_id_for("call_exec")


def test_the_child_closed_with_its_trace_has_its_turn_and_a_sane_end(
        codex_home, tmp_path, sent):
    child, rows = _after_a_sweep(codex_home, tmp_path, sent)
    root_id = spans.span_id_for("subagent:" + child)
    turns = [s for s in rows.values() if s.name == "turn" and s.parent_span_id == root_id]
    assert len(turns) == 1 and not turns[0].pending
    assert all(s.end_ns >= s.start_ns for s in rows.values() if not s.pending)
    assert not any(s.pending for s in rows.values())


def test_a_child_no_call_can_be_found_for_hangs_under_its_spawners_open_turn(
        codex_home, tmp_path, sent):
    child, rows = _after_a_sweep(codex_home, tmp_path, sent, with_exec=False)
    root = rows[spans.span_id_for("subagent:" + child)]
    assert not root.pending
    assert root.parent_span_id == spans.span_id_for("turn:t1")


def test_a_child_with_no_turn_to_hang_under_hangs_under_the_session_root(
        codex_home, tmp_path, sent):
    folder = tmp_path / "sessions"
    folder.mkdir()
    parent = _rollout(folder / ("rollout-p-%s.jsonl" % PARENT), [_meta(PARENT, ms=-100)])
    child, child_path = _child(folder, PARENT)
    _hooks(tmp_path, parent, child, child_path)
    last_ns = _state(tmp_path)["last_ns"]
    del sent[:]
    _sweep(tmp_path, last_ns + 2 * HOUR_NS)
    rows = {s.span_id: s for _, out in sent for s in out}
    root = rows[spans.span_id_for("subagent:" + child)]
    assert root.parent_span_id == spans.span_id_for("session:" + PARENT)


def test_a_stopped_session_sends_nothing_more_when_it_is_closed(
        codex_home, tmp_path, sent):
    folder = tmp_path / "sessions"
    parent = _dying_parent(folder)
    child, child_path = _child(folder, PARENT)
    _hooks(tmp_path, parent, child, child_path)
    stopped = _state(tmp_path)
    stopped["content_stopped"] = True
    state.save(PARENT, str(tmp_path), stopped)
    del sent[:]
    _sweep(tmp_path, stopped["last_ns"] + 2 * HOUR_NS)
    rows = {s.span_id: s for _, out in sent for s in out}
    assert spans.span_id_for("subagent:" + child) not in rows
    assert rows[spans.span_id_for("session:" + PARENT)].pending is False


def test_finalize_places_a_waiting_child_directly(tmp_path):
    from rius_cc import codex_script  # noqa: F401
    folder = tmp_path / "sessions"
    parent = _dying_parent(folder)
    child, child_path = _child(folder, PARENT)
    st = codex_session.load({})
    codex_session.note_payload(st, "SubagentStop", {
        "transcript_path": str(parent), "agent_id": child, "agent_type": "default",
        "agent_transcript_path": str(child_path)})
    ctx = spans.Ctx(session_id=PARENT, cwd="/tmp/proj", git_branch="", cc_version="",
                    service_name="codex", capture_content=False, max_attr_bytes=32768)
    codex_session.build(st, ctx, str(parent))
    assert st["spawned"] == {} and st["codex_subs"][child]["state"] is None
    out = codex_session.finalize(st, ctx, T0 * 10**6 + 10**9)
    assert st["spawned"] == {child: spans.span_id_for("call_exec")}
    assert st["codex_subs"][child]["state"] is not None
    assert any(s.name == "default" and not s.pending for s in out)


def _append(path, *lines):
    with open(str(path), "a") as fh:
        fh.write("\n".join(lines) + "\n")


def test_a_grandchild_is_placed_by_the_state_of_its_spawner_as_it_is_now(tmp_path):
    """The grandchild's hook is read before the child's rollout is: the child
    is read first, so its open spawn call is seen and the grandchild waits for
    the line that names it, not hung under a finished exec of the child."""
    folder = tmp_path / "sessions"
    folder.mkdir()
    c_id, g_id = _uuid7_at(T0 + 40, "c" * 19), _uuid7_at(T0 + 200, "d" * 19)

    def v2_spawn(ms, call_id):
        return _item(ms, {"type": "function_call", "name": "spawn_agent",
                          "namespace": "collaboration", "call_id": call_id,
                          "arguments": "{}"})

    def started(ms, call_id, agent_id):
        return _event(ms, {"type": "sub_agent_activity", "event_id": call_id,
                           "agent_thread_id": agent_id, "kind": "started"})
    root = _rollout(folder / ("rollout-p-%s.jsonl" % PARENT), [
        _meta(PARENT, ms=-100), _event(T0, {"type": "task_started", "turn_id": "t1"}),
        v2_spawn(T0 + 10, "call_c"), started(T0 + 50, "call_c", c_id),
        _item(T0 + 60, {"type": "function_call_output", "call_id": "call_c",
                        "output": '{"task_name":"/root/c"}'})])
    c_path = _rollout(folder / ("rollout-c-%s.jsonl" % c_id), [
        _meta(c_id, PARENT, 41), _event(T0 + 42, {"type": "task_started", "turn_id": "tc"}),
        _exec_call(T0 + 100, "call_cexec", NO_TOOL_SCRIPT), _exec_done(T0 + 110, "call_cexec")])
    g_path = _rollout(folder / ("rollout-g-%s.jsonl" % g_id), [
        _meta(g_id, c_id, 201), _event(T0 + 202, {"type": "task_started", "turn_id": "tg"}),
        _event(T0 + 400, {"type": "task_complete", "turn_id": "tg",
                          "last_agent_message": ""})])
    ctx = spans.Ctx(session_id=PARENT, cwd="/tmp/proj", git_branch="", cc_version="",
                    service_name="codex", capture_content=False, max_attr_bytes=32768)
    st = codex_session.load({})
    st["transcript_path"] = str(root)

    def step():
        out, st["offset"] = codex_session.build(st, ctx, str(root))
        return codex_session._spawned(st)
    assert step() == {c_id: spans.span_id_for("call_c")}

    _append(c_path, v2_spawn(T0 + 150, "call_d"))       # begun, not yet named
    codex_session.note_payload(st, "SubagentStop", {
        "transcript_path": str(c_path), "agent_id": g_id, "agent_type": "default",
        "agent_transcript_path": str(g_path)})
    assert g_id not in step()

    _append(c_path, started(T0 + 210, "call_d", g_id), _item(T0 + 220, {
        "type": "function_call_output", "call_id": "call_d", "output": "{}"}))
    assert step()[g_id] == spans.span_id_for("call_d")


def test_a_grandchild_of_a_child_not_yet_read_hangs_under_that_child_when_closing(
        codex_home, tmp_path, sent):
    """Both are known only by their hooks when the trace is closed: the child
    is read first and the grandchild placed under its open turn, not under the
    root's."""
    folder = tmp_path / "sessions"
    parent = _dying_parent(folder)
    c_id = _uuid7_at(T0 + 40, "c" * 19)
    g_id = _uuid7_at(T0 + 200, "d" * 19)
    c_path = _rollout(folder / ("rollout-c-%s.jsonl" % c_id), [
        _meta(c_id, PARENT, 41), _event(T0 + 42, {"type": "task_started", "turn_id": "tc"})])
    g_path = _rollout(folder / ("rollout-g-%s.jsonl" % g_id), [
        _meta(g_id, c_id, 201), _event(T0 + 202, {"type": "task_started", "turn_id": "tg"})])
    for event, extra in (("UserPromptSubmit", {}),
                         ("SubagentStop", {"agent_id": c_id, "agent_type": "default",
                                           "agent_transcript_path": str(c_path)}),
                         ("SubagentStop", {"agent_id": g_id, "agent_type": "default",
                                           "agent_transcript_path": str(g_path)}),
                         ("Stop", {})):
        payload = {"session_id": PARENT, "cwd": "/tmp/proj", "hook_event_name": event,
                   "transcript_path": str(parent)}
        payload.update(extra)
        exporter.run(event, payload, ENV, str(tmp_path))
    del sent[:]
    _sweep(tmp_path, _state(tmp_path)["last_ns"] + 2 * HOUR_NS)
    rows = {s.span_id: s for _, out in sent for s in out}
    parent_of = {agent: rows[spans.span_id_for("subagent:" + agent)].parent_span_id
                 for agent in (c_id, g_id)}
    assert parent_of == {c_id: spans.span_id_for("call_exec"),
                         g_id: spans.span_id_for("turn:tc")}
    assert not any(s.pending for s in rows.values())


def test_a_child_whose_spawner_is_unknown_hangs_under_the_session_when_closing(
        codex_home, tmp_path, sent):
    folder = tmp_path / "sessions"
    parent = _dying_parent(folder, with_exec=False)
    child, child_path = _child(folder, _uuid7_at(T0 - 500, "e" * 19))
    _hooks(tmp_path, parent, child, child_path)
    del sent[:]
    _sweep(tmp_path, _state(tmp_path)["last_ns"] + 2 * HOUR_NS)
    rows = {s.span_id: s for _, out in sent for s in out}
    root = rows[spans.span_id_for("subagent:" + child)]
    assert root.parent_span_id == spans.span_id_for("turn:t1") and not root.pending
