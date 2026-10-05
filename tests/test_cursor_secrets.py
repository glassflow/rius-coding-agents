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
