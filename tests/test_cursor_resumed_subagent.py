"""A Task subagent started in a `cursor-agent -p --resume` run.

A resumed run fires no sessionStart, so no hook is handed the session env
that names the chat (real_headless_resume has none on its second run). The
subagent's events then name nobody, and its parent is found from what the
spools say: a chat in this workspace whose Task call no subagent answers yet.

The payloads are the real subagent run (tests/fixtures/cursor), rearranged
as the second run of a chat: its sessionStart is moved to a first run of its
own and `_hook_env` is dropped from the rest. That arrangement is derived,
not captured.
"""
import json
import os
import pathlib
import subprocess
import sys
import threading
import time

import pytest

from rius_cc import (agent, config, cursor_events, cursor_export, cursor_hook,
                     cursor_spans, platform_compat)

from . import signed_in
from .cursor_fixtures import STEP_NS, clock, payloads, resumed_subagent_run
from .platforms import posix_only

SCRIPTS = str(pathlib.Path(__file__).parent.parent / "scripts")

WORKSPACE = "/Users/dev/project"
OTHER = "/Users/dev/other"
PARENT = "c0de000c-0000-4000-8000-00000000000d"
CHILD = "c0de000e-0000-4000-8000-00000000000f"
TASK_CALL = "tool_abc00000000000000000000000000000000"
STRANGER = "c0de0000-0000-4000-8000-0000000000dd"


def _until_the_task_call():
    """The resumed run up to the Task call, before its subagent's first
    event."""
    run = resumed_subagent_run()
    return run[:next(i for i, e in enumerate(run)
                     if e.get("tool_name") == "Task") + 1]


def _recent_clock():
    """Every hook fired within the last minute, as the link window needs."""
    return clock(time.time_ns() - 60 * STEP_NS)


def _spool(events, sdir, workspace=WORKSPACE):
    """Spool the way the hook does, each with the env it was handed."""
    tick = _recent_clock()
    for payload in events:
        payload = dict(payload)
        env = payload.pop("_hook_env", {})
        cursor_hook.link_headless_subagent(
            payload["hook_event_name"], payload["conversation_id"], env,
            str(sdir), workspace)
        cursor_events.record(payload, str(sdir), True, 32768, clock=tick)


def _named(out, name):
    return [s for s in out if s.name == name]


def _link(sdir, conversation_id):
    return cursor_events.read_link(
        cursor_export._sidecar(str(sdir), conversation_id,
                               cursor_export.PARENT_SUFFIX))


def _holder(sdir, call, parent=PARENT):
    """Who holds a Task call, "" when nobody does."""
    try:
        with open(cursor_export._claim_path(str(sdir), parent, call)) as fh:
            return fh.read()
    except OSError:
        return ""


def _first_event_of(conversation_id, **changes):
    """The real subagent's first event, as another conversation's."""
    first = dict(payloads("real_headless_subagent")[4],
                 conversation_id=conversation_id,
                 generation_id=conversation_id, session_id=conversation_id)
    first.update(changes)
    first.pop("_hook_env", None)
    return first


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


def test_a_task_call_is_found_by_the_workspace_it_runs_in(tmp_path):
    sdir = str(tmp_path)
    _spool(_until_the_task_call(), tmp_path)
    found = cursor_export.claim_awaiting_task_call(sdir, WORKSPACE, CHILD)
    assert found == (PARENT, TASK_CALL)
    assert cursor_export.claim_awaiting_task_call(sdir, OTHER, CHILD) is None
    assert cursor_export.claim_awaiting_task_call(sdir, "", CHILD) is None


def test_a_tool_that_ran_elsewhere_does_not_move_the_chat(tmp_path):
    sdir = str(tmp_path)
    elsewhere = dict(_until_the_task_call()[-2], cwd=OTHER, tool_name="Shell",
                     hook_event_name="preToolUse", tool_use_id="shell-1")
    _spool(_until_the_task_call() + [elsewhere], tmp_path)
    assert cursor_export.claim_awaiting_task_call(sdir, OTHER, CHILD) is None
    cursor_hook.link_headless_subagent("preToolUse", STRANGER, {}, sdir, OTHER)
    assert cursor_events.linked_children(sdir, PARENT) == []


def test_a_chat_that_ended_has_no_task_call_waiting(tmp_path):
    ended = [e for e in resumed_subagent_run() if e["conversation_id"] == PARENT]
    _spool(ended, tmp_path)
    assert ended[-1]["hook_event_name"] == "sessionEnd"
    assert cursor_export.claim_awaiting_task_call(
        str(tmp_path), WORKSPACE, CHILD) is None


def test_a_finished_task_call_is_not_waiting(tmp_path):
    sdir = str(tmp_path)
    _spool(_until_the_task_call(), tmp_path)
    assert cursor_export.claim_awaiting_task_call(sdir, WORKSPACE, CHILD)
    done = dict(_until_the_task_call()[-1], hook_event_name="postToolUse")
    cursor_events.record(done, sdir, True, 32768)
    assert cursor_export.claim_awaiting_task_call(sdir, WORKSPACE, CHILD) is None


def test_a_task_call_older_than_the_window_is_not_waiting(tmp_path):
    sdir = str(tmp_path)
    _spool(_until_the_task_call(), tmp_path)
    began = cursor_events.read_spool(
        cursor_events.spool_path(sdir, PARENT))[-1]["ts"]
    window = cursor_export.TASK_LINK_WINDOW_NS
    assert cursor_export.claim_awaiting_task_call(
        sdir, WORKSPACE, CHILD, began + window - STEP_NS)
    assert cursor_export.claim_awaiting_task_call(
        sdir, WORKSPACE, CHILD, began + window + STEP_NS) is None


def test_the_window_is_about_two_minutes():
    assert cursor_export.TASK_LINK_WINDOW_NS == 120 * 10**9


def test_the_newest_chat_with_a_task_call_waiting_wins(tmp_path):
    sdir = str(tmp_path)
    _spool(_until_the_task_call(), tmp_path)
    older = "c0de0000-0000-4000-8000-0000000000aa"
    tick = _recent_clock()
    for event in _until_the_task_call():
        cursor_events.record(dict(event, conversation_id=older), sdir, True,
                             32768, clock=tick)
    stale = time.time() - 30
    os.utime(cursor_events.spool_path(sdir, older), (stale, stale))
    found = cursor_export.claim_awaiting_task_call(sdir, WORKSPACE, CHILD)
    assert found[0] == PARENT


def test_only_the_newest_chats_are_searched(tmp_path):
    sdir = str(tmp_path)
    _spool(_until_the_task_call(), tmp_path)
    parent_spool = cursor_events.spool_path(sdir, PARENT)
    stale = time.time() - 60
    os.utime(parent_spool, (stale, stale))
    for i in range(cursor_export.TASK_LINK_CANDIDATES):
        other = "c0de0000-0000-4000-8000-%012d" % i
        cursor_events.record({"conversation_id": other, "workspace_roots": [OTHER],
                              "hook_event_name": "sessionStart"}, sdir, True,
                             32768)
    assert cursor_export.claim_awaiting_task_call(sdir, WORKSPACE, CHILD) is None
    os.remove(cursor_events.spool_path(sdir, "c0de0000-0000-4000-8000-%012d" % 0))
    assert cursor_export.claim_awaiting_task_call(sdir, WORKSPACE, CHILD)


def test_a_task_call_starts_one_subagent(tmp_path):
    """`-p --resume X` for a chat with no spool, while another chat's Task
    call already has its subagent, must not become that chat's second."""
    sdir = str(tmp_path)
    _spool(resumed_subagent_run()[:-1], tmp_path)
    assert (_link(sdir, CHILD), _holder(sdir, TASK_CALL)) == (PARENT, CHILD)
    cursor_hook.link_headless_subagent("preToolUse", STRANGER, {}, sdir,
                                       WORKSPACE)
    assert cursor_events.linked_children(sdir, PARENT) == [CHILD]
    assert cursor_export.claim_awaiting_task_call(
        sdir, WORKSPACE, STRANGER) is None


def test_parallel_task_calls_each_start_one_subagent(tmp_path):
    sdir = str(tmp_path)
    second = dict(_until_the_task_call()[-1], tool_use_id="tool_second")
    first_child = "c0de0000-0000-4000-8000-0000000000a1"
    second_child = "c0de0000-0000-4000-8000-0000000000b1"
    _spool(_until_the_task_call() + [second, _first_event_of(first_child),
                                     _first_event_of(second_child)], tmp_path)
    assert _holder(sdir, TASK_CALL) == first_child
    assert _holder(sdir, "tool_second") == second_child
    cursor_hook.link_headless_subagent("preToolUse", STRANGER, {}, sdir,
                                       WORKSPACE)
    assert cursor_events.linked_children(sdir, PARENT) == [first_child,
                                                           second_child]


def test_a_subagent_the_env_names_claims_its_task_call_too(tmp_path):
    sdir = str(tmp_path)
    real = payloads("real_headless_subagent")
    _spool(real[:5], tmp_path)
    assert real[4]["conversation_id"] == CHILD
    assert (_link(sdir, CHILD), _holder(sdir, TASK_CALL)) == (PARENT, CHILD)
    cursor_hook.link_headless_subagent("preToolUse", STRANGER, {}, sdir,
                                       WORKSPACE)
    assert cursor_events.linked_children(sdir, PARENT) == [CHILD]


def test_a_subagent_start_claims_its_task_call(tmp_path):
    sdir = str(tmp_path)
    started = {"conversation_id": PARENT, "hook_event_name": "subagentStart",
               "subagent_id": CHILD, "tool_call_id": "tool_9"}
    config_for = type("Cfg", (), {"capture_content": True,
                                  "max_attr_bytes": 32768})
    cursor_hook._spool("subagentStart", started, sdir, config_for)
    assert (_link(sdir, CHILD), _holder(sdir, "tool_9")) == (PARENT, CHILD)


def test_a_resumed_chat_with_a_spool_is_not_taken_for_a_subagent(tmp_path):
    sdir = str(tmp_path)
    _spool(_until_the_task_call(), tmp_path)
    other = "c0de0000-0000-4000-8000-0000000000cc"
    open(cursor_events.spool_path(sdir, other), "w").close()
    cursor_hook.link_headless_subagent("preToolUse", other, {}, sdir, WORKSPACE)
    assert cursor_events.linked_children(sdir, PARENT) == []


def test_a_session_env_that_names_a_missing_parent_is_not_second_guessed(tmp_path):
    sdir = str(tmp_path)
    _spool(_until_the_task_call(), tmp_path)
    gone = "c0de0000-0000-4000-8000-0000000000ee"
    cursor_hook.link_headless_subagent(
        "preToolUse", CHILD, {cursor_hook.SESSION_ENV: gone}, sdir, WORKSPACE)
    assert cursor_events.linked_children(sdir, PARENT) == []


@pytest.mark.parametrize("odd, as_text", [
    (["a"], "['a']"), ({"a": 1}, "{'a': 1}"), (7, "7"),
    ("two\nlines", "two lines")])
def test_a_tool_call_id_of_any_type_is_read_as_text(tmp_path, odd, as_text):
    sdir = str(tmp_path)
    _spool(_until_the_task_call()[:-1]
           + [dict(_until_the_task_call()[-1], tool_use_id=odd)], tmp_path)
    events = cursor_events.read_spool(cursor_events.spool_path(sdir, PARENT))
    assert list(cursor_export._open_task_calls(events)) == [as_text]
    assert cursor_export.claim_awaiting_task_call(
        sdir, WORKSPACE, CHILD) == (PARENT, as_text)


def test_a_failing_search_links_nothing_and_raises_nothing(tmp_path, monkeypatch):
    sdir = str(tmp_path)
    _spool(_until_the_task_call(), tmp_path)

    def broken(*args, **kwargs):
        raise RuntimeError("unreadable spool")

    monkeypatch.setattr(cursor_events, "read_spool", broken)
    assert cursor_export.claim_awaiting_task_call(sdir, WORKSPACE, CHILD) is None
    assert cursor_export.claim_oldest_task_call(sdir, PARENT, CHILD) == ""
    cursor_hook.link_headless_subagent("preToolUse", STRANGER, {}, sdir,
                                       WORKSPACE)
    assert cursor_events.linked_children(sdir, PARENT) == []


def test_a_link_is_replaced_whole_and_leaves_no_temp_file(tmp_path):
    sdir = str(tmp_path)
    cursor_export.link_subagent(sdir, CHILD, PARENT)
    cursor_export.link_subagent(sdir, CHILD, "other-parent")
    assert _link(sdir, CHILD) == "other-parent"
    assert [n for n in os.listdir(sdir) if n.endswith(".tmp")] == []


def test_a_link_that_cannot_be_written_leaves_the_old_one_intact(tmp_path,
                                                                 monkeypatch):
    sdir = str(tmp_path)
    cursor_export.link_subagent(sdir, CHILD, PARENT)

    def refuse(src, dst, *args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(platform_compat, "replace_atomic", refuse)
    with pytest.raises(OSError):
        cursor_export.link_subagent(sdir, CHILD, "other-parent")
    assert _link(sdir, CHILD) == PARENT
    assert [n for n in os.listdir(sdir) if n.endswith(".tmp")] == []


def test_a_link_without_a_task_call_reads_as_before(tmp_path):
    sdir = str(tmp_path)
    with open(os.path.join(sdir, CHILD + ".parent"), "w") as fh:
        fh.write(PARENT)
    assert _link(sdir, CHILD) == PARENT
    assert cursor_export.root_conversation(sdir, CHILD) == PARENT


def test_a_link_with_a_second_line_still_names_its_parent(tmp_path):
    sdir = str(tmp_path)
    with open(os.path.join(sdir, CHILD + ".parent"), "w") as fh:
        fh.write(PARENT + "\n" + TASK_CALL)
    assert _link(sdir, CHILD) == PARENT
    assert cursor_events.linked_children(sdir, PARENT) == [CHILD]


def test_every_event_records_the_workspace_root_apart_from_the_tool_cwd():
    shell = dict(next(e for e in payloads("real_headless_subagent")
                      if e.get("tool_name") == "Shell"), cwd=OTHER)
    record = cursor_events.to_record(shell, 1, False, 1024)
    assert (record["cwd"], record["workspace"]) == (OTHER, WORKSPACE)


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


# --- through cursor_hook.handle, the way a hook process calls it ------------

@pytest.fixture
def home(tmp_path):
    with agent.using(agent.CURSOR):
        yield tmp_path / "home"


def _set_up(home, signed_in_here=True, enabled=True):
    if signed_in_here:
        signed_in.sign_in(str(home))
    if enabled:
        config.write_path_rules(str(home), {"enabled_paths": [WORKSPACE]})


def _fire(home, events, extra_env=None):
    for payload in events:
        payload = dict(payload)
        env = dict(extra_env or {}, **payload.pop("_hook_env", {}))
        cursor_hook.handle(payload["hook_event_name"], payload, env, str(home))


def _spool_text(home):
    sdir = cursor_export.spool_dir(str(home))
    text = ""
    for name in os.listdir(sdir):
        with open(os.path.join(sdir, name), encoding="utf-8") as fh:
            text += fh.read()
    return text


def test_the_hook_links_a_subagent_of_a_resumed_run(home):
    _set_up(home)
    _fire(home, resumed_subagent_run())
    events = cursor_export.read_events(str(home), PARENT)
    out = cursor_spans.build(events, cursor_spans.Ctx(PARENT, True, 32768))
    assert _named(out, "explore")[0].parent_span_id == _named(out, "Task")[0].span_id


def test_the_hook_links_it_with_content_capture_off(home):
    _set_up(home)
    _fire(home, resumed_subagent_run(), {"RIUS_CAPTURE_CONTENT": "false"})
    sdir = cursor_export.spool_dir(str(home))
    assert cursor_events.linked_children(sdir, PARENT) == [CHILD]
    assert "<redacted content>" not in _spool_text(home)
    events = cursor_export.read_events(str(home), PARENT)
    out = cursor_spans.build(events, cursor_spans.Ctx(PARENT, False, 32768))
    assert _named(out, "explore")[0].parent_span_id == _named(out, "Task")[0].span_id


@pytest.mark.parametrize("signed_in_here, enabled", [(True, False), (False, True)])
def test_a_folder_that_is_off_or_a_signed_out_machine_searches_nothing(
        home, monkeypatch, signed_in_here, enabled):
    _set_up(home, signed_in_here, enabled)
    sdir = cursor_export.spool_dir(str(home))
    _spool(_until_the_task_call(), sdir)
    reads = []
    real = cursor_events.read_spool
    monkeypatch.setattr(cursor_events, "read_spool",
                        lambda *a, **k: reads.append(a) or real(*a, **k))
    _fire(home, [_first_event_of(CHILD)])
    assert reads == []
    assert cursor_events.linked_children(sdir, PARENT) == []


def test_a_signed_out_machine_writes_no_link_even_when_the_env_names_a_parent(home):
    _set_up(home, signed_in_here=False)
    sdir = cursor_export.spool_dir(str(home))
    _spool(_until_the_task_call(), sdir)
    _fire(home, [dict(_first_event_of(CHILD))],
          {cursor_hook.SESSION_ENV: PARENT})
    assert cursor_events.linked_children(sdir, PARENT) == []


def test_a_folder_that_is_off_still_follows_an_env_that_names_a_parent(home):
    _set_up(home, enabled=False)
    sdir = cursor_export.spool_dir(str(home))
    _spool(_until_the_task_call(), sdir)
    _fire(home, [dict(_first_event_of(CHILD))],
          {cursor_hook.SESSION_ENV: PARENT})
    assert cursor_events.linked_children(sdir, PARENT) == [CHILD]


def test_a_chat_turned_off_keeps_its_subagents_untraced_too(home):
    _set_up(home)
    sdir = cursor_export.spool_dir(str(home))
    _spool(_until_the_task_call(), sdir)
    env = {cursor_hook.SESSION_ENV: PARENT}
    config.set_session_override(PARENT, str(home), False)
    _fire(home, [_first_event_of(CHILD)], env)
    assert not os.path.exists(cursor_events.spool_path(sdir, CHILD))
    config.set_session_override(PARENT, str(home), None)
    _fire(home, [_first_event_of(CHILD, tool_use_id="again")], env)
    assert os.path.exists(cursor_events.spool_path(sdir, CHILD))


def _resumed_chat_event():
    """The first event of the real resumed run's second run: a chat that
    already has a transcript."""
    real = payloads("real_headless_resume")
    ended = next(i for i, e in enumerate(real)
                 if e["hook_event_name"] == "sessionEnd")
    event = dict(real[ended + 1])
    assert event["transcript_path"]
    return event


def test_a_chat_with_a_transcript_is_a_resumed_chat_not_a_subagent(home):
    _set_up(home)
    sdir = cursor_export.spool_dir(str(home))
    _spool(_until_the_task_call(), sdir)
    _fire(home, [_resumed_chat_event()])
    assert cursor_events.linked_children(sdir, PARENT) == []


def test_the_same_event_without_a_transcript_is_a_subagent(home):
    _set_up(home)
    sdir = cursor_export.spool_dir(str(home))
    _spool(_until_the_task_call(), sdir)
    new = dict(_resumed_chat_event(), transcript_path=None,
               conversation_id=STRANGER, session_id=STRANGER)
    _fire(home, [new])
    assert cursor_events.linked_children(sdir, PARENT) == [STRANGER]


# --- claims ------------------------------------------------------------------

def test_a_task_call_goes_to_one_subagent_and_stays_with_it(tmp_path):
    sdir = str(tmp_path)
    assert cursor_export.claim_task_call(sdir, PARENT, "call-1", CHILD)
    assert not cursor_export.claim_task_call(sdir, PARENT, "call-1", STRANGER)
    assert cursor_export.claim_task_call(sdir, PARENT, "call-1", CHILD)
    assert cursor_export.claim_task_call(sdir, PARENT, "call-2", STRANGER)


def test_a_claim_still_being_written_is_read_again(tmp_path):
    sdir = str(tmp_path)
    path = cursor_export._claim_path(sdir, PARENT, "call-1")
    open(path, "w").close()
    writer = threading.Timer(0.02, lambda: open(path, "w").write(CHILD))
    writer.start()
    try:
        assert cursor_export.claim_task_call(sdir, PARENT, "call-1", CHILD)
    finally:
        writer.join()


def test_a_claim_that_stays_empty_is_nobodys(tmp_path):
    sdir = str(tmp_path)
    open(cursor_export._claim_path(sdir, PARENT, "call-1"), "w").close()
    assert not cursor_export.claim_task_call(sdir, PARENT, "call-1", CHILD)


RACE = """
import json, sys, time
sys.path.insert(0, sys.argv[1])
from rius_cc import agent, cursor_hook
agent.activate(agent.CURSOR)
home, payload, start = sys.argv[2], json.loads(sys.argv[3]), float(sys.argv[4])
while time.time() < start:
    pass
cursor_hook.handle(payload["hook_event_name"], payload, {}, home)
"""


@posix_only("starts hook processes that must run at the same moment")
def test_two_subagents_starting_together_never_share_a_task_call(tmp_path):
    first_child = "c0de0000-0000-4000-8000-0000000000a1"
    second_child = "c0de0000-0000-4000-8000-0000000000b1"
    second_call = dict(_until_the_task_call()[-1], tool_use_id="tool_second")
    trials = [tmp_path / ("home%d" % i) for i in range(12)]
    for home in trials:
        with agent.using(agent.CURSOR):
            _set_up(home)
            _spool(_until_the_task_call() + [second_call],
                   cursor_export.spool_dir(str(home)))
    start = time.time() + 4
    runs = [subprocess.Popen(
        [sys.executable, "-c", RACE, SCRIPTS, str(home),
         json.dumps(_first_event_of(child)), str(start)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for home in trials for child in (first_child, second_child)]
    for run in runs:
        _, err = run.communicate(timeout=120)
        assert run.returncode == 0, err
    for home in trials:
        with agent.using(agent.CURSOR):
            sdir = cursor_export.spool_dir(str(home))
        # Both calls are taken, so a third chat finds none to attach to.
        cursor_hook.link_headless_subagent("preToolUse", STRANGER, {}, sdir,
                                           WORKSPACE)
        assert cursor_events.linked_children(sdir, PARENT) == [first_child,
                                                               second_child]
        held = {_holder(sdir, TASK_CALL), _holder(sdir, "tool_second")}
        assert held == {first_child, second_child}


# --- what is cleaned up with the spool ----------------------------------------

def test_claims_go_with_their_conversations_spool(tmp_path):
    sdir = str(tmp_path)
    _spool(_until_the_task_call(), tmp_path)
    cursor_export.claim_task_call(sdir, PARENT, TASK_CALL, CHILD)
    claim = cursor_export._claim_path(sdir, PARENT, TASK_CALL)
    later = time.time() + cursor_export.SPOOL_RETENTION_S + 60
    for path in os.listdir(sdir):
        os.utime(os.path.join(sdir, path), (later - 1e9, later - 1e9))
    assert cursor_export.prune(sdir, [PARENT], now_s=later) == 0
    assert os.path.exists(claim)
    assert cursor_export.prune(sdir, [], now_s=later) == 2
    assert not os.path.exists(claim)
    assert os.listdir(sdir) == []


def test_a_claim_is_kept_while_its_conversation_is_recent(tmp_path):
    sdir = str(tmp_path)
    _spool(_until_the_task_call(), tmp_path)
    cursor_export.claim_task_call(sdir, PARENT, TASK_CALL, CHILD)
    assert cursor_export.prune(sdir, []) == 0


def test_a_link_temp_file_a_killed_hook_left_goes_after_a_while(tmp_path):
    sdir = str(tmp_path)
    old = os.path.join(sdir, ".link-abc.tmp")
    new = os.path.join(sdir, ".link-def.tmp")
    for path in (old, new):
        open(path, "w").close()
    now = time.time()
    os.utime(old, (now - cursor_export.LINK_TEMP_TTL_S - 60,) * 2)
    assert cursor_export.prune(sdir, [], now_s=now) == 1
    assert os.listdir(sdir) == [".link-def.tmp"]
