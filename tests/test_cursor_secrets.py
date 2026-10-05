"""Cursor content mode removes secrets the way Claude Code's does: every
content attribute and error line is scrubbed, and a read of a secret-shaped
file (.env, *.pem, ...) is replaced whole."""
import json

from rius_cc import cursor_events, cursor_spans, scrub

AWS_KEY = "AKIA" + "ABCDEFGHIJKLMNOP"
CID = "c0ffee00-0000-4000-8000-0000000000aa"
GEN = "gen-secret-1"


def _payloads():
    def ev(name, **fields):
        fields.update(hook_event_name=name, conversation_id=CID,
                      generation_id=fields.get("generation_id", GEN),
                      model="claude-sonnet-4")
        return fields

    def tool(tool_id, name, tool_input, **post):
        hook = post.pop("hook", "postToolUse")
        return [ev("preToolUse", tool_use_id=tool_id, tool_name=name,
                   tool_input=tool_input),
                ev(hook, tool_use_id=tool_id, tool_name=name,
                   tool_input=tool_input, **post)]

    return ([ev("sessionStart", generation_id=CID, session_id=CID),
             ev("beforeSubmitPrompt", prompt="deploy with " + AWS_KEY)]
            + tool("t-read", "Read", {"path": "/repo/.env"},
                   tool_output="ONLY_IN_THE_FILE=1")
            + tool("t-cat", "Shell", {"command": "cat .env"},
                   tool_output=json.dumps({"output": "ONLY_IN_THE_FILE=1",
                                           "exitCode": 0}))
            + tool("t-cat-fail", "Shell", {"command": "grep X .env"},
                   tool_output=json.dumps({"output": "ONLY_IN_THE_FILE=2",
                                           "exitCode": 1}))
            + tool("t-fail", "Shell", {"command": "deploy"},
                   tool_output=json.dumps({"output": "error: bad key " + AWS_KEY,
                                           "exitCode": 1}))
            + tool("t-denied", "Write", {"path": "/repo/a.py"},
                   hook="postToolUseFailure", failure_type="error",
                   error_message="refused: password=hunter2hunter2")
            + [ev("afterAgentResponse", text="used " + AWS_KEY),
               ev("stop", status="completed", loop_count=0),
               ev("sessionEnd", generation_id=CID, session_id=CID,
                  reason="error", final_status="error",
                  error_message="crashed holding " + AWS_KEY)])


def _build(tmp_path):
    for payload in _payloads():
        cursor_events.record(payload, str(tmp_path), True, 32768)
    events = cursor_events.read_conversation(str(tmp_path), CID)
    ctx = cursor_spans.Ctx(CID, capture_content=True, max_attr_bytes=32768)
    return cursor_spans.build(events, ctx)


def _everything_sent(span):
    return json.dumps([span.attributes, span.status_message,
                       [list(e) for e in span.events or []]], default=str)


def test_no_secret_leaves_in_any_attribute_status_or_event(tmp_path):
    out = _build(tmp_path)
    sent = "".join(_everything_sent(s) for s in out)
    assert "used " in sent and "deploy with " in sent      # content is on
    assert AWS_KEY not in sent
    assert "hunter2hunter2" not in sent
    assert "ONLY_IN_THE_FILE" not in sent


def test_a_secret_file_read_is_replaced_whole(tmp_path):
    tools = {s.attributes.get("gen_ai.tool.call.id"): s for s in _build(tmp_path)}
    for tool_id in ("t-read", "t-cat"):
        assert tools[tool_id].attributes["output.value"] == scrub.SECRET_FILE_MARKER
    failed = tools["t-cat-fail"]
    assert failed.status_message == "Shell.exit_1"
    assert failed.events[0][2]["exception.message"] == "Shell.exit_1"


def test_error_lines_are_scrubbed_not_dropped(tmp_path):
    out = _build(tmp_path)
    tools = {s.attributes.get("gen_ai.tool.call.id"): s for s in out}
    assert tools["t-fail"].status_message == "error: bad key [redacted:aws-key]"
    assert tools["t-denied"].status_message == "refused: password=[redacted:password]"
    root = next(s for s in out if s.name == cursor_spans.DEFAULT_ROOT_NAME)
    assert root.status_message == "crashed holding [redacted:aws-key]"


ENV_FILE = ("DATABASE_URL=postgres://admin:Sup3rS3cret@db.internal:5432/prod\n"
            "SMTP_PASS=hunter2hunter2\n")


def _shell_call(tmp_path, command):
    def ev(name, **fields):
        fields.update(hook_event_name=name, conversation_id=CID,
                      generation_id=CID, model="default")
        return fields

    tool_input = {"command": command, "cwd": "", "timeout": 30000}
    output = json.dumps({"output": ENV_FILE, "exitCode": 0})
    for payload in (ev("sessionStart", session_id=CID),
                    ev("preToolUse", tool_use_id="t1", tool_name="Shell",
                       tool_input=tool_input),
                    ev("postToolUse", tool_use_id="t1", tool_name="Shell",
                       tool_input=tool_input, tool_output=output),
                    ev("afterShellExecution", command=command, output=ENV_FILE),
                    ev("sessionEnd", session_id=CID, reason="completed")):
        path = cursor_events.record(payload, str(tmp_path), True, 32768)
    with open(path, encoding="utf-8") as fh:
        spooled = fh.read()
    events = cursor_events.read_conversation(str(tmp_path), CID)
    out = cursor_spans.build(events, cursor_spans.Ctx(CID, True, 32768))
    return spooled, "".join(_everything_sent(s) for s in out)


def test_a_quoted_secret_word_in_a_command_keeps_the_secret_file_rule(tmp_path):
    """Scrubbing the spooled input cut a value at an escaped quote, the JSON
    no longer parsed, and the `.env` read went out scrubbed only."""
    for command in ('grep -v "password:" .env', 'curl -H "token: abc" x; cat .env',
                    'cat .env | grep "secret: x"', 'grep -n "api_key=" .env'):
        spooled, sent = _shell_call(tmp_path / str(len(command)), command)
        assert "Sup3rS3cret" not in sent, command
        assert scrub.SECRET_FILE_MARKER in sent, command


def test_a_secret_file_read_never_reaches_the_spool(tmp_path):
    spooled, _ = _shell_call(tmp_path, "cat .env")
    assert "Sup3rS3cret" not in spooled
    assert "hunter2hunter2" not in spooled
    lines = [json.loads(line) for line in spooled.splitlines()]
    shell = [line for line in lines if line["event"] == "afterShellExecution"]
    assert shell[0]["output"] == scrub.SECRET_FILE_MARKER
    assert shell[0]["command"] == "cat .env"


def test_a_key_shaped_subagent_type_is_not_kept(tmp_path):
    for kind in ("sk-ant-api03-" + "A" * 26, "ghp_" + "a" * 36,
                 "a" * 33, "explore me"):
        payload = {"hook_event_name": "preToolUse", "conversation_id": CID,
                   "tool_name": "Task", "tool_use_id": "t",
                   "tool_input": {"subagent_type": kind, "prompt": "x"}}
        assert cursor_events.task_subagent_type(payload) == "", kind
        path = cursor_events.record(payload, str(tmp_path), False, 32768)
        with open(path, encoding="utf-8") as fh:
            assert kind not in fh.read()
    for kind in ("explore", "generalPurpose", "code-reviewer"):
        payload = {"tool_name": "Task", "tool_input": {"subagent_type": kind}}
        assert cursor_events.task_subagent_type(payload) == kind


def test_a_huge_output_is_spooled_within_the_hook_timeout(tmp_path):
    """Only what is kept is scrubbed: scrubbing all 20 MB took 6 s, past
    Cursor's 5 s hook timeout."""
    import time
    payload = {"hook_event_name": "postToolUse", "conversation_id": CID,
               "tool_name": "Shell", "tool_use_id": "t",
               "tool_input": {"command": "yes"},
               "tool_output": "x" * (20 << 20) + " " + AWS_KEY}
    started = time.perf_counter()
    path = cursor_events.record(payload, str(tmp_path), True, 32768)
    assert time.perf_counter() - started < 1.5
    with open(path, encoding="utf-8") as fh:
        line = json.loads(fh.read())
    assert len(line["tool_output"].encode("utf-8")) < 32768 + 64
