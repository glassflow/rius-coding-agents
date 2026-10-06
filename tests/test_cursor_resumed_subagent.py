"""A Task subagent started in a `cursor-agent -p --resume` run.

A resumed run fires no sessionStart, so no hook is handed the session env
that names the chat (real_headless_resume has none on its second run). The
subagent's events then name nobody, and the parent is found from what the
spool says: the chat in this workspace that is waiting on a Task call.

The payloads are the real subagent run (tests/fixtures/cursor), rearranged
as the second run of a chat: its sessionStart is moved to a first run of its
own and `_hook_env` is dropped from the rest. That arrangement is derived,
not captured.
"""
import os
import time

from rius_cc import cursor_events, cursor_export, cursor_hook, cursor_spans

from .cursor_fixtures import clock, payloads, resumed_subagent_run

WORKSPACE = "/Users/dev/project"
PARENT = "c0de000c-0000-4000-8000-00000000000d"
CHILD = "c0de000e-0000-4000-8000-00000000000f"


def _until_the_task_call():
    """The resumed run up to the Task call, before its subagent's first
    event."""
    run = resumed_subagent_run()
    return run[:next(i for i, e in enumerate(run)
                     if e.get("tool_name") == "Task") + 1]


def _spool(events, sdir):
    """Spool the way the hook does, each with the env it was handed."""
    tick = clock()
    for payload in events:
        payload = dict(payload)
        env = payload.pop("_hook_env", {})
        cursor_hook.link_headless_subagent(
            payload["hook_event_name"], payload["conversation_id"], env,
            str(sdir), WORKSPACE)
        cursor_events.record(payload, str(sdir), True, 32768, clock=tick)


def _named(out, name):
    return [s for s in out if s.name == name]


def test_a_subagent_of_a_resumed_run_hangs_under_its_task_call(tmp_path):
    _spool(resumed_subagent_run(), tmp_path)
    assert cursor_events.linked_children(str(tmp_path), PARENT) == [CHILD]
    events = cursor_events.read_conversation(str(tmp_path), PARENT)
    out = cursor_spans.build(events, cursor_spans.Ctx(PARENT, True, 32768))
    task = _named(out, "Task")[0]
    subagent = _named(out, "explore")[0]
    assert subagent.parent_span_id == task.span_id
    assert {s.name for s in out if s.parent_span_id == subagent.span_id} == {
        "Grep", "Read", "Shell"}
    assert len({s.trace_id for s in out}) == 1


def test_the_subagent_belongs_to_the_resumed_run_not_the_first(tmp_path):
    _spool(resumed_subagent_run(), tmp_path)
    events = cursor_events.read_conversation(str(tmp_path), PARENT)
    out = cursor_spans.build(events, cursor_spans.Ctx(PARENT, True, 32768))
    first_end, second_end = [e["ts"] for e in events
                             if e["event"] == "sessionEnd"]
    subagent = _named(out, "explore")[0]
    assert first_end < subagent.start_ns <= subagent.end_ns <= second_end
    assert not any(s.pending for s in out)


def test_a_chat_waiting_on_a_task_call_is_found_by_its_workspace(tmp_path):
    sdir = str(tmp_path)
    _spool(_until_the_task_call(), tmp_path)
    assert cursor_export.conversation_in_task_call(sdir, WORKSPACE) == PARENT
    assert cursor_export.conversation_in_task_call(sdir, "/Users/dev/other") == ""
    assert cursor_export.conversation_in_task_call(sdir, "") == ""


def test_a_chat_that_ended_is_not_waiting_on_a_task_call(tmp_path):
    sdir = str(tmp_path)
    ended = [e for e in resumed_subagent_run() if e["conversation_id"] == PARENT]
    _spool(ended, tmp_path)
    assert ended[-1]["hook_event_name"] == "sessionEnd"
    assert cursor_export.conversation_in_task_call(sdir, WORKSPACE) == ""


def test_a_finished_task_call_is_not_waited_on(tmp_path):
    sdir = str(tmp_path)
    run = [e for e in resumed_subagent_run()[:-1] if e["conversation_id"] == PARENT]
    task = next(e for e in run if e.get("tool_name") == "Task")
    _spool(run, tmp_path)
    assert cursor_export.conversation_in_task_call(sdir, WORKSPACE) == PARENT
    done = dict(task, hook_event_name="postToolUse")
    cursor_events.record(done, sdir, True, 32768)
    assert cursor_export.conversation_in_task_call(sdir, WORKSPACE) == ""


def test_a_chat_idle_for_longer_than_the_window_is_not_waited_on(tmp_path):
    sdir = str(tmp_path)
    _spool(_until_the_task_call(), tmp_path)
    later = time.time() + cursor_export.TASK_LINK_WINDOW_S + 60
    assert cursor_export.conversation_in_task_call(sdir, WORKSPACE, later) == ""


def test_the_newest_chat_waiting_on_a_task_call_wins(tmp_path):
    sdir = str(tmp_path)
    _spool(_until_the_task_call(), tmp_path)
    older = "c0de0000-0000-4000-8000-0000000000aa"
    for event in _until_the_task_call():
        cursor_events.record(dict(event, conversation_id=older), sdir, True,
                             32768)
    stale = time.time() - 120
    os.utime(cursor_events.spool_path(sdir, older), (stale, stale))
    assert cursor_export.conversation_in_task_call(sdir, WORKSPACE) == PARENT


def test_a_resumed_chat_with_a_spool_is_not_taken_for_a_subagent(tmp_path):
    sdir = str(tmp_path)
    _spool(_until_the_task_call(), tmp_path)
    other = "c0de0000-0000-4000-8000-0000000000cc"
    open(cursor_events.spool_path(sdir, other), "w").close()
    cursor_hook.link_headless_subagent("preToolUse", other, {}, sdir, WORKSPACE)
    assert cursor_events.linked_children(sdir, PARENT) == []


def test_a_conversation_in_another_workspace_is_not_linked(tmp_path):
    sdir = str(tmp_path)
    _spool(_until_the_task_call(), tmp_path)
    stranger = "c0de0000-0000-4000-8000-0000000000dd"
    cursor_hook.link_headless_subagent("preToolUse", stranger, {}, sdir,
                                       "/Users/dev/other")
    assert cursor_events.linked_children(sdir, PARENT) == []


def test_a_session_env_that_names_a_parent_is_never_second_guessed(tmp_path):
    sdir = str(tmp_path)
    _spool(_until_the_task_call(), tmp_path)
    gone = "c0de0000-0000-4000-8000-0000000000ee"
    cursor_hook.link_headless_subagent(
        "preToolUse", CHILD, {cursor_hook.SESSION_ENV: gone}, sdir, WORKSPACE)
    assert cursor_events.linked_children(sdir, PARENT) == []


def test_every_event_records_the_workspace_when_the_tool_cwd_is_empty():
    shell = next(e for e in payloads("real_headless_subagent")
                 if e.get("tool_name") == "Shell")
    assert shell["cwd"] == ""
    record = cursor_events.to_record(shell, 1, False, 1024)
    assert record["cwd"] == WORKSPACE


def test_a_spool_can_be_read_from_its_tail(tmp_path):
    path = str(tmp_path / "s.jsonl")
    tick = clock()
    for i in range(50):
        cursor_events.record({"conversation_id": "s", "hook_event_name": "stop",
                              "loop_count": i}, str(tmp_path), False, 1024,
                             clock=tick)
    whole = cursor_events.read_spool(path)
    tail = cursor_events.read_spool(path, tail_bytes=300)
    assert 0 < len(tail) < len(whole)
    assert tail == whole[-len(tail):]
