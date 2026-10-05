"""The span tree from hook payloads captured in real cursor-agent sessions
(tests/fixtures/cursor/real_*.json, redacted).

What the docs did not say: `cursor-agent -p` fires no prompt, stop or
subagent hooks; tool hooks carry the conversation id as their generation
id; afterAgentThought names a turn `<id>-<step>-<random>`; a Task
subagent's events arrive under its own conversation id with nothing naming
the parent; and one tool_use_id can cover two calls.
"""
import json
import os

from rius_cc import cursor_events, cursor_hook, cursor_spans

from .cursor_fixtures import clock, payloads


def _spool(name, spool_dir, capture=True):
    """Spool a fixture the way the hook does, session env included."""
    tick = clock()
    events = payloads(name)
    for payload in events:
        payload = dict(payload)
        env = payload.pop("_hook_env", {})
        cursor_hook.link_headless_subagent(
            payload["hook_event_name"], payload["conversation_id"], env,
            str(spool_dir))
        cursor_events.record(payload, str(spool_dir), capture, 32768,
                             clock=tick)
    return events[0]["conversation_id"]


def _build(tmp_path, name, capture=True):
    cid = _spool(name, tmp_path, capture)
    events = cursor_events.read_conversation(str(tmp_path), cid)
    ctx = cursor_spans.Ctx(cid, capture_content=capture, max_attr_bytes=32768)
    return events, cursor_spans.build(events, ctx)


def _named(out, name):
    return [s for s in out if s.name == name]


def _children(out, parent):
    return [s for s in out if s.parent_span_id == parent.span_id]


def _session_end_times(events):
    return [e["ts"] for e in events if e["event"] == "sessionEnd"]


def test_a_headless_run_is_one_turn_holding_its_tools(tmp_path):
    events, out = _build(tmp_path, "real_headless_shell")
    root = _named(out, "cursor session")[0]
    turns = _named(out, "turn")
    assert len(turns) == 1
    turn = turns[0]
    assert turn.parent_span_id == root.span_id
    assert turn.end_ns == _session_end_times(events)[0]
    assert turn.status_code == "OK"
    assert {s.name for s in _children(out, turn)} == {
        "cursor-grok-4.5-high-fast", "Shell"}
    assert len(_named(out, "Shell")) == 2
    assert not any(s.pending for s in out)


def test_the_llm_span_takes_its_provider_from_model_id(tmp_path):
    _, out = _build(tmp_path, "real_headless_shell")
    llm = _named(out, "cursor-grok-4.5-high-fast")[0]
    assert llm.attributes["gen_ai.provider.name"] == "x_ai"
    assert llm.attributes["gen_ai.request.model"] == "cursor-grok-4.5-high-fast"
    assert not any(k.startswith("gen_ai.usage") for k in llm.attributes)


def test_a_failed_shell_call_is_an_error_without_an_exit_code(tmp_path):
    _, out = _build(tmp_path, "real_headless_shell", capture=False)
    failed = [s for s in _named(out, "Shell") if s.status_code == "ERROR"]
    assert len(failed) == 1
    assert failed[0].attributes["error.type"] == "Shell.error"


def test_a_resumed_chat_adds_a_turn_without_stretching_the_first(tmp_path):
    events, out = _build(tmp_path, "real_headless_resume")
    first_end, second_end = _session_end_times(events)
    turns = sorted(_named(out, "turn"), key=lambda s: s.start_ns)
    assert [t.end_ns for t in turns] == [first_end, second_end]
    assert len({s.trace_id for s in out}) == 1
    assert _named(out, "cursor session")[0].end_ns == second_end


def test_one_tool_use_id_for_a_read_and_a_write_keeps_both(tmp_path):
    _, out = _build(tmp_path, "real_headless_edit")
    tools = sorted((s for s in out if s.kind_oi == "TOOL"),
                   key=lambda s: s.start_ns)
    assert [s.name for s in tools] == ["Read", "Read", "Write"]
    assert len({s.span_id for s in tools}) == 3
    assert all(s.status_code == "OK" and not s.pending for s in tools)


def test_a_headless_subagent_hangs_under_its_task_call(tmp_path):
    _, out = _build(tmp_path, "real_headless_subagent")
    task = _named(out, "Task")[0]
    subagent = _named(out, "explore")[0]
    assert subagent.parent_span_id == task.span_id
    assert subagent.attributes["cursor.subagent.id"].startswith("c0de")
    assert {s.name for s in _children(out, subagent)} == {
        "Grep", "Read", "Shell"}
    assert len(_named(out, "turn")) == 1
    assert len({s.trace_id for s in out}) == 1
    assert not any(s.pending for s in out)


def test_a_task_call_with_no_end_hook_closes_with_unknown_outcome(tmp_path):
    _, out = _build(tmp_path, "real_headless_subagent")
    task = _named(out, "Task")[0]
    subagent = _named(out, "explore")[0]
    assert task.attributes["cursor.tool.closed_at_session_end"] is True
    assert task.status_code == "UNSET"
    assert task.end_ns == subagent.end_ns


def test_a_subagent_is_linked_only_from_its_parent_session(tmp_path):
    sdir = str(tmp_path)
    parent = "c0de0000-0000-4000-8000-0000000000aa"
    child = "c0de0000-0000-4000-8000-0000000000bb"
    env = {cursor_hook.SESSION_ENV: parent}
    parent_spool = cursor_events.spool_path(sdir, parent)
    cursor_hook.link_headless_subagent("preToolUse", child, env, sdir)
    assert cursor_events.linked_children(sdir, parent) == []

    open(parent_spool, "w").close()
    for event in ("beforeSubmitPrompt", "sessionStart", "sessionEnd"):
        cursor_hook.link_headless_subagent(event, child, env, sdir)
    cursor_hook.link_headless_subagent("preToolUse", parent, env, sdir)
    assert cursor_events.linked_children(sdir, parent) == []

    cursor_hook.link_headless_subagent("preToolUse", child, env, sdir)
    assert cursor_events.linked_children(sdir, parent) == [child]


def test_a_conversation_already_spooled_is_never_relinked(tmp_path):
    sdir = str(tmp_path)
    parent = "c0de0000-0000-4000-8000-0000000000aa"
    other = "c0de0000-0000-4000-8000-0000000000cc"
    open(cursor_events.spool_path(sdir, parent), "w").close()
    open(cursor_events.spool_path(sdir, other), "w").close()
    cursor_hook.link_headless_subagent(
        "preToolUse", other, {cursor_hook.SESSION_ENV: parent}, sdir)
    assert cursor_events.linked_children(sdir, parent) == []


def test_an_interactive_prompt_can_arrive_before_session_start(tmp_path):
    events, out = _build(tmp_path, "real_interactive_error")
    assert events[0]["event"] == "beforeSubmitPrompt"
    root = _named(out, "cursor session")[0]
    assert root.start_ns == events[0]["ts"]
    turns = _named(out, "turn")
    assert len(turns) == 2
    assert all(t.status_code == "ERROR" for t in turns)
    assert all(t.attributes["input.value"] == "<redacted content>" for t in turns)


def test_capture_off_spools_no_content_from_real_payloads(tmp_path):
    for name in ("real_headless_shell", "real_headless_edit",
                 "real_headless_subagent", "real_interactive_error"):
        _spool(name, tmp_path / name, capture=False)
    for folder, _, files in os.walk(str(tmp_path)):
        for name in files:
            with open(os.path.join(folder, name), encoding="utf-8") as fh:
                text = fh.read()
            assert "<redacted content>" not in text, name
            assert "dev@example.com" not in text, name


def test_the_spool_holds_no_secret_with_capture_on(tmp_path):
    payload = dict(payloads("real_headless_shell")[3])
    assert payload["hook_event_name"] == "postToolUseFailure"
    payload["error_message"] = "aws_access_key_id = AKIAABCDEFGHIJKLMNOP"
    payload["tool_input"] = {"command": "cat creds AKIAABCDEFGHIJKLMNOP"}
    path = cursor_events.record(payload, str(tmp_path), True, 32768)
    with open(path, encoding="utf-8") as fh:
        line = json.loads(fh.read())
    assert "AKIAABCDEFGHIJKLMNOP" not in json.dumps(line)
    assert "aws_access_key_id" in line["error_message"]
