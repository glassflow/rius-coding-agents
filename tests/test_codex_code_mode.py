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
    state["tool_starts"] = [[100, "a"], [200, "b"], [300, "c"]]
    assert codex_spans.spawning_tool(state, 250) == "b"
    assert codex_spans.spawning_tool(state, 50) is None


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
