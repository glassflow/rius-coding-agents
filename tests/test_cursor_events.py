import json
import os
import stat
import sys

import pytest

from rius_cc import cursor_events

from .cursor_fixtures import BASE_NS, payloads, spool


def _payload(**kw):
    base = {"hook_event_name": "postToolUse", "conversation_id": "conv-1",
            "generation_id": "gen-1", "model": "claude-4.5-sonnet",
            "user_email": "dev@example.com",
            "transcript_path": "/home/dev/.cursor/projects/x/t.jsonl",
            "workspace_roots": ["/home/dev/proj"]}
    base.update(kw)
    return base


def _lines(path):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def test_record_appends_one_stamped_line_per_hook(tmp_path):
    path = cursor_events.record(_payload(tool_name="Read", tool_use_id="t1"),
                                str(tmp_path), True, 32768, clock=lambda: 42)
    cursor_events.record(_payload(hook_event_name="stop", status="completed"),
                         str(tmp_path), True, 32768, clock=lambda: 43)

    lines = _lines(path)
    assert [(l["ts"], l["event"]) for l in lines] == [(42, "postToolUse"), (43, "stop")]
    assert lines[0]["tool_use_id"] == "t1"
    assert lines[0]["cwd"] == "/home/dev/proj"
    assert path == os.path.join(str(tmp_path), "conv-1.jsonl")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
def test_spool_is_private_even_if_it_existed_wider(tmp_path):
    path = os.path.join(str(tmp_path), "conv-1.jsonl")
    with open(path, "w"):
        pass
    os.chmod(path, 0o644)

    cursor_events.record(_payload(), str(tmp_path), True, 32768)

    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_who_and_where_the_transcript_is_are_never_kept(tmp_path):
    path = cursor_events.record(_payload(), str(tmp_path), True, 32768)
    blob = open(path, encoding="utf-8").read()
    assert "dev@example.com" not in blob
    assert "t.jsonl" not in blob


def test_unknown_fields_are_dropped():
    rec = cursor_events.to_record(_payload(brand_new_field="secret stuff"), 1,
                                  True, 32768)
    assert "brand_new_field" not in rec


def test_capture_off_keeps_identity_and_drops_content():
    payload = _payload(tool_name="Shell", tool_use_id="t1", duration=12,
                       tool_input={"command": "cat ~/.netrc"},
                       tool_output=json.dumps({"output": "machine x password y",
                                               "exitCode": 2}))
    rec = cursor_events.to_record(payload, 1, False, 32768)
    assert rec["tool_name"] == "Shell"
    assert rec["duration"] == 12
    assert rec["exit_code"] == 2
    assert "tool_input" not in rec and "tool_output" not in rec


def test_content_is_capped_at_max_attr_bytes():
    rec = cursor_events.to_record(_payload(tool_output="x" * 5000), 1, True, 100)
    assert rec["tool_output"].startswith("x" * 100)
    assert "truncated 4900 bytes" in rec["tool_output"]


def test_object_content_is_stored_as_json_text():
    rec = cursor_events.to_record(_payload(tool_input={"path": "a.py"}), 1, True, 32768)
    assert rec["tool_input"] == '{"path": "a.py"}'


@pytest.mark.parametrize("output, code", [
    ('{"output": "ok", "exitCode": 0}', 0),
    ({"output": "boom", "exitCode": 127}, 127),
    ('{"exit_code": 3}', 3),
    ("plain text", None),
    ('{"exitCode": true}', None),
    (None, None),
])
def test_shell_exit_code(output, code):
    assert cursor_events.shell_exit_code(output) == code


def test_subagent_lifecycle_spools_under_the_parent(tmp_path):
    path = cursor_events.record(
        _payload(hook_event_name="subagentStart", conversation_id="sub-9",
                 parent_conversation_id="conv-1", subagent_id="sub-9"),
        str(tmp_path), True, 32768)
    assert os.path.basename(path) == "conv-1.jsonl"


@pytest.mark.parametrize("cid", ["../../etc/passwd", "a/b", ".hidden", ""])
def test_a_hostile_conversation_id_cannot_escape_the_spool(tmp_path, cid):
    path = cursor_events.record(_payload(conversation_id=cid), str(tmp_path),
                                True, 32768)
    if not cid:
        assert path is None
        return
    assert os.path.dirname(path) == str(tmp_path)
    assert not os.path.basename(path).startswith(".")


def test_garbage_payloads_are_ignored_quietly(tmp_path):
    assert cursor_events.record("not a dict", str(tmp_path), True, 1) is None
    assert cursor_events.record({"hook_event_name": "stop"}, str(tmp_path), True, 1) is None
    assert os.listdir(str(tmp_path)) == []


def test_an_unwritable_spool_never_raises(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("")
    assert cursor_events.record(_payload(), str(blocker / "spool"), True, 1) is None


def test_a_torn_last_line_is_skipped(tmp_path):
    path = cursor_events.record(_payload(), str(tmp_path), True, 32768)
    with open(path, "a") as fh:
        fh.write('{"ts": 9, "event": "sto')
    assert [e["event"] for e in cursor_events.read_spool(path)] == ["postToolUse"]


def test_read_conversation_merges_subagent_spools_in_time_order(tmp_path):
    cid = spool("docs_session", tmp_path)
    events = cursor_events.read_conversation(str(tmp_path), cid)

    assert len(events) == len(payloads("docs_session"))
    assert [e["ts"] for e in events] == sorted(e["ts"] for e in events)
    assert events[0]["ts"] == BASE_NS
    grep = [e for e in events if e.get("tool_use_id") == "tool-grep-1"]
    assert {e["conversation_id"] for e in grep} == {"5ub00000-0000-4000-8000-000000000002"}


@pytest.mark.parametrize("event", ["sessionStart", "preToolUse", "beforeSubmitPrompt",
                                   "stop", "subagentStop", "subagentStart"])
def test_the_hook_answer_never_blocks_or_steers(event):
    answer = json.loads(cursor_events.response_for(event))
    assert answer == {}
