"""Codex code mode, from a real codex-cli 0.144.1 run against gpt-5.6-luna.

`code_mode_host` is on by default: the model calls one `exec` tool that runs
a script, and the script calls exec_command, spawn_agent and wait_agent. So
the rollout has no spawn_agent call to hang a subagent under, and a tool's
output is a list of content parts rather than a string.

fixtures/codex/code_mode/ is the parent and child rollout of a turn that ran
`ls /nonexistent`, spawned a `default` subagent (which ran `ls`) and waited
for it. Every prompt, reply, script and output text is stripped.
"""
import json
import pathlib
import shutil

from rius_cc import codex_rollout, codex_spans, spans

from tests.test_codex_export import ENV, codex_home, exporter, sent  # noqa: F401

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "codex" / "code_mode"
PARENT = "01a10cd7-ea95-7313-8660-2f7de512f6ab"
CHILD = "01a10cd8-06a5-7a10-b31d-e02818200c2d"
SPAWN_CALL = "call_561c3bcd895043eb8ced222d97355b41"
# The rollouts' final total_token_usage.
PARENT_TOTALS = (54593, 37120, 291, 86)
CHILD_TOTALS = (24608, 20992, 85, 0)


def _rollouts(tmp_path):
    folder = tmp_path / "sessions"
    shutil.copytree(str(FIXTURES), str(folder))
    return {p.stem[-36:]: p for p in folder.glob("rollout-*.jsonl")}


def _run(home, event, transcript, **extra):
    payload = {"session_id": PARENT, "cwd": "/tmp/proj",
               "transcript_path": str(transcript), "hook_event_name": event}
    payload.update(extra)
    return exporter.run(event, payload, ENV, str(home))


def _hooks_as_codex_fired_them(home, rollouts):
    """The hook sequence of the real run, payloads trimmed to what matters."""
    parent, child = rollouts[PARENT], rollouts[CHILD]
    _run(home, "UserPromptSubmit", parent)
    _run(home, "PostToolUse", parent, tool_name="Bash")
    _run(home, "PostToolUse", parent, tool_name="spawn_agent",
         tool_response=json.dumps({"agent_id": CHILD, "nickname": "x"}))
    _run(home, "PostToolUse", child, agent_id=CHILD, agent_type="default",
         tool_name="Bash")
    _run(home, "SubagentStop", parent, agent_id=CHILD, agent_type="default",
         agent_transcript_path=str(child))
    _run(home, "PostToolUse", parent, tool_name="multi_agent_v1wait_agent")
    _run(home, "Stop", parent)


def _finished(batches):
    return {s.span_id: s for _, out in batches for s in out if not s.pending}


def _latest(batches):
    """The last row sent per span id, pending or not: a subagent's root
    stays open until the session is closed."""
    return {s.span_id: s for _, out in batches for s in out}


def _totals(llm_spans):
    keys = ("gen_ai.usage.input_tokens", "gen_ai.usage.cache_read.input_tokens",
            "gen_ai.usage.output_tokens", "gen_ai.usage.reasoning.output_tokens")
    return tuple(sum(s.attributes.get(k) or 0 for s in llm_spans) for k in keys)


def _under(finished, root_id):
    """Every finished span below root_id."""
    below, frontier = [], {root_id}
    while frontier:
        kids = [s for s in finished.values() if s.parent_span_id in frontier]
        below += kids
        frontier = {s.span_id for s in kids}
    return below


def test_a_code_mode_subagent_hangs_under_the_exec_call_that_spawned_it(
        codex_home, tmp_path, sent):
    _hooks_as_codex_fired_them(tmp_path, _rollouts(tmp_path))
    finished = _finished(sent)
    sub_root = _latest(sent).get(spans.span_id_for("subagent:" + CHILD))
    assert sub_root is not None, "the subagent was never exported"
    assert sub_root.parent_span_id == spans.span_id_for(SPAWN_CALL)
    assert finished[sub_root.parent_span_id].name == "exec"
    assert sub_root.name == "default"
    child_llm = [s for s in _under(finished, sub_root.span_id)
                 if s.kind_oi == "LLM"]
    assert _totals(child_llm) == CHILD_TOTALS


def test_every_token_of_parent_and_child_is_exported_once(
        codex_home, tmp_path, sent):
    _hooks_as_codex_fired_them(tmp_path, _rollouts(tmp_path))
    finished = _finished(sent)
    llm = [s for s in finished.values() if s.kind_oi == "LLM"]
    both = tuple(p + c for p, c in zip(PARENT_TOTALS, CHILD_TOTALS))
    assert _totals(llm) == both
    rows = [s.span_id for _, out in sent for s in out if not s.pending]
    assert len(rows) == len(set(rows))


def test_subagent_stop_alone_is_enough_to_link_the_child(
        codex_home, tmp_path, sent):
    rollouts = _rollouts(tmp_path)
    _run(tmp_path, "SubagentStop", rollouts[PARENT], agent_id=CHILD,
         agent_type="default", agent_transcript_path=str(rollouts[CHILD]))
    _run(tmp_path, "Stop", rollouts[PARENT])
    sub_root = _latest(sent).get(spans.span_id_for("subagent:" + CHILD))
    assert sub_root is not None
    assert sub_root.parent_span_id == spans.span_id_for(SPAWN_CALL)


def test_the_spawning_call_is_the_last_one_begun_before_the_child_existed():
    state = codex_spans.new_state()
    state["tool_starts"] = [[100, "a", "exec"], [200, "b", "exec"],
                            [300, "c", "exec"]]
    assert codex_spans.spawning_tool(state, 250) == "b"
    assert codex_spans.spawning_tool(state, 50) is None


def test_only_an_exec_call_is_placed_by_time():
    """A spawn_agent call's output names its agent exactly; by time, two
    spawns in parallel would both land under the second."""
    state = codex_spans.new_state()
    state["tool_starts"] = [[100, "a", "exec"],
                            [200, "b", codex_spans.SPAWN_TOOL],
                            [300, "c", "exec_command"]]
    assert codex_spans.spawning_tool(state, 250) is None
    assert codex_spans.spawning_tool(state, 350) is None
    assert codex_spans.spawning_tool(state, 150) == "a"


def test_exec_output_parts_are_read_as_their_text():
    line = json.dumps({
        "timestamp": "2026-10-05T16:14:03.381Z", "type": "response_item",
        "payload": {"type": "custom_tool_call_output", "call_id": "c1",
                    "output": [
                        {"type": "input_text",
                         "text": "Script completed\nOutput:\n"},
                        {"type": "input_text", "text": "line one\nline two\n"}]}})
    record = codex_rollout.parse_line(line)
    assert record.get("output") == "Script completed\nOutput:\nline one\nline two\n"


def test_a_secret_in_exec_output_parts_is_removed():
    """The real run's file: dumped as JSON, the parts turned each newline
    into a literal `\\n`, so the key id's value ran on into the next line
    and swallowed the `aws_secret_access_key` name that would have flagged
    the secret after it."""
    from rius_cc import scrub
    key_id = "AKIA" + "ABCDEFGHIJKLMNOP"
    secret = "wJalrXUtnFEMI" + "/K7MDENG/bPxRfiCYEXAMPLEKEY"
    line = json.dumps({
        "timestamp": "2026-10-05T16:14:03.381Z", "type": "response_item",
        "payload": {"type": "custom_tool_call_output", "call_id": "c1",
                    "output": [
                        {"type": "input_text", "text": "Output:\n"},
                        {"type": "input_text",
                         "text": "aws_access_key_id = %s\n"
                                 "aws_secret_access_key = %s\n" % (key_id, secret)}]}})
    output = codex_rollout.parse_line(line).get("output")
    assert secret not in scrub.scrub(output)


KEY_ID = "AKIA" + "ABCDEFGHIJKLMNOP"
AWS_SECRET = "wJalrXUtnFEMI" + "/K7MDENG/bPxRfiCYEXAMPLEKEY"
CREDENTIALS = ("[default]\naws_access_key_id = %s\naws_secret_access_key = %s\n"
               % (KEY_ID, AWS_SECRET))
ENV_FILE = ("DATABASE_URL=postgres://admin:S3cr3tPw@db.internal:5432/prod\n"
            "INTERNAL_HOST=db.internal.acme\n")


def _exec_output(script, texts):
    """One `exec` call and its output parts, built into a tool span with
    content on."""
    lines = [
        {"timestamp": "2026-10-05T16:14:00.000Z", "type": "session_meta",
         "payload": {"id": PARENT, "cwd": "/tmp/proj",
                     "cli_version": "0.144.1"}},
        {"timestamp": "2026-10-05T16:14:00.100Z", "type": "event_msg",
         "payload": {"type": "task_started", "turn_id": "t1"}},
        {"timestamp": "2026-10-05T16:14:00.200Z", "type": "turn_context",
         "payload": {"turn_id": "t1", "model": "gpt-5.6-luna"}},
        {"timestamp": "2026-10-05T16:14:03.000Z", "type": "response_item",
         "payload": {"type": "custom_tool_call", "call_id": "c1",
                     "name": "exec", "input": script}},
        {"timestamp": "2026-10-05T16:14:03.381Z", "type": "response_item",
         "payload": {"type": "custom_tool_call_output", "call_id": "c1",
                     "output": [{"type": "input_text", "text": t}
                                for t in texts]}}]
    records = [codex_rollout.parse_line(json.dumps(line)) for line in lines]
    assert None not in records
    ctx = spans.Ctx(session_id=PARENT, cwd="/tmp/proj", git_branch="",
                    cc_version="", service_name="codex",
                    capture_content=True, max_attr_bytes=32768)
    out = codex_spans.build(records, codex_spans.new_state(), ctx)
    tool = [s for s in out if s.name == "exec" and not s.pending][0]
    return tool.attributes["output.value"]


def test_each_exec_output_part_is_its_own_line():
    """`text(value)` appends one part and no newline: run together, the
    key id's line swallows the next part's `aws_secret_access_key`."""
    value = _exec_output("text(r.output)", [
        "Script completed\nOutput:\n", "[default]",
        "aws_access_key_id = " + KEY_ID,
        "aws_secret_access_key = " + AWS_SECRET])
    assert AWS_SECRET not in value
    assert "aws_secret_access_key = [redacted:" in value


def test_a_secret_in_json_inside_exec_output_is_removed():
    """`text(JSON.stringify(result))`: each line break is a literal `\\n`."""
    value = _exec_output("text(JSON.stringify(s))", [
        "Script completed\nOutput:\n",
        json.dumps({"status": {"x": {"completed": CREDENTIALS}}})])
    assert AWS_SECRET not in value
    assert KEY_ID not in value


def test_a_secret_file_read_inside_exec_is_replaced_whole():
    """Code mode runs `cat .env` from a script, not from JSON arguments."""
    for script in ('const r = await tools.exec_command({cmd: "cat .env"});'
                   'text(r.output);',
                   "text(await tools.exec_command({cmd: 'cat ~/.aws/credentials'}))",
                   'const p = `${home}/.ssh/id_rsa`; text(await read(p));'):
        value = _exec_output(script, ["Script completed\nOutput:\n", ENV_FILE])
        assert value == "[redacted:secret-file]", script


def test_a_script_naming_no_secret_file_keeps_its_output():
    value = _exec_output(
        'const k = obj.key; text(process.env.HOME); text("ls -la")',
        ["Script completed\nOutput:\n", "INTERNAL_HOST=db.internal.acme\n"])
    assert "INTERNAL_HOST=db.internal.acme" in value
