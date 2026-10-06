"""Codex code mode, from a real codex-cli 0.144.1 run against gpt-5.6-luna.

`code_mode_host` is on by default: the model calls one `exec` tool that runs
a script, and the script calls exec_command, spawn_agent and wait_agent. So
the rollout has no spawn_agent call to hang a subagent under, and a tool's
output is a list of content parts rather than a string.

fixtures/codex/code_mode/ is the parent and child rollout of a turn that ran
`ls /nonexistent`, spawned a `default` subagent (which ran `ls`) and waited
for it. Every prompt, reply, script and output text is stripped.
"""
import datetime
import json
import pathlib
import shutil

import pytest

from rius_cc import codex_rollout, codex_spans, spans

from tests.test_codex_export import (ENV, _uuid7_at, codex_home,  # noqa: F401
                                     exporter, sent)

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


SPAWN_SCRIPT = ("const r = await tools.multi_agent_v1__spawn_agent({message: 'x'});"
                "text(r.agent_id);")
SHELL_SCRIPT = 'text((await tools.exec_command({cmd: "sleep 5"})).output);'
WAIT_SCRIPT = "await tools.multi_agent_v1__wait_agent({targets: [id]});"
NO_TOOL_SCRIPT = "text(new Date().toISOString())"


def _record(kind, ns, **fields):
    return codex_rollout.Record(kind, ns, fields)


def _ctx(capture=False):
    return spans.Ctx(session_id=PARENT, cwd="/tmp/proj", git_branch="",
                     cc_version="", service_name="codex",
                     capture_content=capture, max_attr_bytes=32768)


def _agent_after(calls):
    """The builder state of an agent that made these exec calls, each as
    (call id, begun, ended or None while it runs, script)."""
    events = [(1, codex_rollout.TURN_START, {"turn_id": "t1"})]
    for call_id, begun, ended, script in calls:
        events.append((begun, codex_rollout.TOOL_CALL,
                       {"call_id": call_id, "name": "exec", "arguments": script}))
        if ended:
            events.append((ended, codex_rollout.TOOL_OUTPUT,
                           {"call_id": call_id, "output": "done"}))
    st = codex_spans.new_state()
    records = [codex_rollout.Record(kind, ns, fields)
               for ns, kind, fields in sorted(events, key=lambda e: e[0])]
    codex_spans.build(records, st, _ctx())
    return st


def _id(call_id):
    return spans.span_id_for(call_id)


def test_the_spawning_call_is_the_exec_running_when_the_child_existed():
    st = _agent_after([("a", 100, 200, SPAWN_SCRIPT), ("b", 300, 400, SPAWN_SCRIPT)])
    assert codex_spans.spawning_tool(st, 150) == _id("a")
    assert codex_spans.spawning_tool(st, 350) == _id("b")
    assert codex_spans.spawning_tool(st, 50) is None


def test_a_child_made_after_every_call_ended_goes_under_the_last_one_begun():
    """A script that kept running after its output came back (Codex yields
    a long one and carries on) has no end to go by."""
    st = _agent_after([("a", 100, 200, SPAWN_SCRIPT), ("b", 300, 400, SPAWN_SCRIPT)])
    assert codex_spans.spawning_tool(st, 250) == _id("a")
    assert codex_spans.spawning_tool(st, 450) == _id("b")


def test_a_call_that_ended_before_the_child_existed_is_passed_over():
    st = _agent_after([("a", 100, 150, SPAWN_SCRIPT), ("b", 120, None, SPAWN_SCRIPT)])
    assert codex_spans.spawning_tool(st, 200) == _id("b")


def test_a_call_that_runs_other_tools_never_takes_the_subagent():
    st = _agent_after([("shell", 100, None, SHELL_SCRIPT),
                       ("a", 200, None, SPAWN_SCRIPT),
                       ("wait", 210, None, WAIT_SCRIPT)])
    assert codex_spans.spawning_tool(st, 250) == _id("a")


def test_a_script_that_names_no_tool_may_have_spawned_through_an_alias():
    st = _agent_after([("a", 100, None, SHELL_SCRIPT), ("b", 200, None, NO_TOOL_SCRIPT)])
    assert codex_spans.spawning_tool(st, 250) == _id("b")


def test_calls_running_together_each_get_a_subagent_in_the_order_they_began():
    st = _agent_after([("a", 100, None, SPAWN_SCRIPT), ("b", 101, None, SPAWN_SCRIPT)])
    for created, child in ((150, "c1"), (160, "c2")):
        st["spawned"][child] = codex_spans.spawning_tool(st, created)
    assert st["spawned"] == {"c1": _id("a"), "c2": _id("b")}


def test_one_call_that_spawns_several_keeps_them_all():
    st = _agent_after([("a", 100, None, SPAWN_SCRIPT)])
    for created, child in ((150, "c1"), (160, "c2"), (170, "c3")):
        st["spawned"][child] = codex_spans.spawning_tool(st, created)
    assert set(st["spawned"].values()) == {_id("a")}


def test_only_an_exec_call_is_placed_by_time():
    """A spawn_agent call's output names its agent exactly; by time, two
    spawns in parallel would both land under the second."""
    st = codex_spans.new_state()
    codex_spans.build([
        _record(codex_rollout.TURN_START, 1, turn_id="t1"),
        _record(codex_rollout.TOOL_CALL, 100, call_id="a",
                name=codex_spans.SPAWN_TOOL, arguments="{}"),
        _record(codex_rollout.TOOL_CALL, 200, call_id="b", name="exec_command",
                arguments="{}")], st, _ctx())
    assert codex_spans.spawning_tool(st, 250) is None


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


def _exec_rows(script, texts, capture=True):
    """One `exec` call and its output parts, built into the tool span's
    pending and finished rows."""
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
                    capture_content=capture, max_attr_bytes=32768)
    out = codex_spans.build(records, codex_spans.new_state(), ctx)
    return [s for s in out if s.kind_oi == "TOOL"]


def _exec_output(script, texts):
    """The output of one `exec` call, built into a tool span with content on."""
    return [s for s in _exec_rows(script, texts)
            if not s.pending][0].attributes["output.value"]


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


def _exec_name(script, capture=True):
    pending, finished = _exec_rows(script, ["Script completed\nOutput:\n"],
                                   capture)
    assert pending.pending and not finished.pending
    assert pending.name == finished.name
    assert finished.attributes["gen_ai.tool.name"] == "exec"
    return finished.name


def test_an_exec_span_is_named_after_the_tools_its_script_calls():
    assert _exec_name('const r = await tools.exec_command({cmd: "ls"});'
                      'text(r.output);') == "exec_command"
    assert _exec_name("text(await tools.apply_patch(patch))") == "apply_patch"
    assert _exec_name("await tools.mcp__rius__list_agents({})") \
        == "mcp__rius__list_agents"


def test_an_exec_that_calls_several_tools_lists_them_once_in_order():
    script = ('await Promise.all([tools.exec_command({cmd: "a"}),'
              ' tools.exec_command({cmd: "b"}), tools.apply_patch(p)]);')
    assert _exec_name(script) == "exec_command, apply_patch"
    many = " ".join("tools.t%d({});" % n for n in range(1, 6))
    assert _exec_name(many) == "t1, t2, t3 +2"


def test_an_exec_that_calls_no_tool_stays_exec():
    assert _exec_name("text(ALL_TOOLS.filter(x => x.name.length > 3))") == "exec"
    assert _exec_name("") == "exec"


def test_what_a_script_says_in_a_string_never_names_the_span():
    assert _exec_name('text("tools.sk_live_1234567890abcdef");') == "exec"
    script = ("const note = 'run tools.deploy_prod now';"
              "await tools.exec_command({cmd: `echo tools.secret_name`});")
    assert _exec_name(script) == "exec_command"


def test_an_exec_span_has_its_name_with_content_off_and_no_content():
    script = 'const r = await tools.exec_command({cmd: "ls /private"});'
    assert _exec_name(script, capture=False) == "exec_command"
    for row in _exec_rows(script, ["Script completed\nOutput:\n"], capture=False):
        assert "input.value" not in row.attributes
        assert "ls /private" not in row.name


def _stamp(ms):
    when = datetime.datetime.fromtimestamp(ms // 1000, datetime.timezone.utc)
    return when.strftime("%Y-%m-%dT%H:%M:%S") + ".%03dZ" % (ms % 1000)


def _line(ms, kind, payload):
    return json.dumps({"timestamp": _stamp(ms), "type": kind, "payload": payload})


def _item(ms, payload):
    return _line(ms, "response_item", payload)


def _event(ms, payload):
    return _line(ms, "event_msg", payload)


def _exec_call(ms, call_id, script):
    return _item(ms, {"type": "custom_tool_call", "call_id": call_id,
                      "name": "exec", "input": script})


def _exec_done(ms, call_id):
    return _item(ms, {"type": "custom_tool_call_output", "call_id": call_id,
                      "output": [{"type": "input_text", "text": "done"}]})


def _turn(ms, turn_id, *middle):
    return ([_event(ms, {"type": "task_started", "turn_id": turn_id})]
            + list(middle)
            + [_event(ms + 900, {"type": "task_complete", "turn_id": turn_id,
                                 "last_agent_message": ""})])


def _rollout(path, lines):
    path.write_text("\n".join(lines) + "\n")
    return path


T0 = 1791216846000


def _concurrent_rollouts(folder):
    """A parent that ran three `exec` calls at once: one runs a command, two
    spawn an agent; and the two children (ids made 20 and 30 ms in)."""
    folder.mkdir()
    first, second = _uuid7_at(T0 + 20, "a" * 19), _uuid7_at(T0 + 30, "b" * 19)
    parent = _rollout(folder / ("rollout-p-%s.jsonl" % PARENT), [
        _line(T0 - 100, "session_meta", {"id": PARENT, "cwd": "/tmp/proj",
                                        "cli_version": "0.144.1"})]
        + _turn(T0, "t1",
                _exec_call(T0 + 1, "call_shell", SHELL_SCRIPT),
                _exec_call(T0 + 2, "call_a", SPAWN_SCRIPT),
                _exec_call(T0 + 3, "call_b", SPAWN_SCRIPT),
                _exec_done(T0 + 500, "call_a"), _exec_done(T0 + 501, "call_b"),
                _exec_done(T0 + 502, "call_shell")))
    children = {}
    for child in (first, second):
        children[child] = _rollout(folder / ("rollout-c-%s.jsonl" % child), [
            _line(T0 + 10, "session_meta", {
                "id": child, "cwd": "/tmp/proj", "cli_version": "0.144.1",
                "source": {"subagent": {"thread_spawn": {
                    "parent_thread_id": PARENT}}}})]
            + _turn(T0 + 40, "t-" + child[:8]))
    return parent, children, first, second


def test_calls_running_together_each_keep_their_own_subagent(
        codex_home, tmp_path, sent):
    """Judged by which call began last, both children would hang under the
    second spawn; judged by time alone, under the command."""
    parent, children, first, second = _concurrent_rollouts(tmp_path / "sessions")
    _run(tmp_path, "UserPromptSubmit", parent)
    for child in (first, second):
        _run(tmp_path, "PostToolUse", parent, tool_name="spawn_agent",
             tool_response=json.dumps({"agent_id": child}))
    _run(tmp_path, "Stop", parent)
    latest = _latest(sent)
    under = {child: latest[spans.span_id_for("subagent:" + child)].parent_span_id
             for child in (first, second)}
    assert under == {first: spans.span_id_for("call_a"),
                     second: spans.span_id_for("call_b")}


def test_children_found_together_are_placed_oldest_first(tmp_path):
    """Both are known by the time the parent is read, the younger's hook
    first: the call that began first still goes to the older child."""
    from rius_cc import codex_session
    parent, children, first, second = _concurrent_rollouts(tmp_path / "sessions")
    st = codex_session.load({})
    for child in (second, first):
        codex_session.note_payload(
            st, "SubagentStop", {"transcript_path": str(parent), "agent_id": child,
                                 "agent_type": "default",
                                 "agent_transcript_path": str(children[child])})
    codex_session.build(st, _ctx(), str(parent))
    assert st["spawned"] == {first: spans.span_id_for("call_a"),
                             second: spans.span_id_for("call_b")}


# --- a subagent is never lost to a guess about what a script can do --------

TEMPLATE_SPAWN = (SHELL_SCRIPT + "\ntext(`child: ${(await tools.multi_agent_v1__"
                  "spawn_agent({message: 'go'})).agent_id}`);")
COMPUTED_SPAWN = (SHELL_SCRIPT + "\nconst r = await tools['multi_agent_v1__"
                  "spawn_agent']({message: 'go'});")
CSV_SPAWN = SHELL_SCRIPT + "\nawait tools.spawn_agents_on_csv({path: 'jobs.csv'});"


@pytest.mark.parametrize("script", [TEMPLATE_SPAWN, COMPUTED_SPAWN, CSV_SPAWN])
def test_a_spawn_the_scanner_does_not_see_still_counts_as_one(script):
    """What a script may do is judged on its whole text: with another tool
    named outside the template, the names found alone said "no spawn" for
    every call, and the subagent and its whole subtree were dropped."""
    st = _agent_after([("shell", 100, None, SHELL_SCRIPT), ("a", 200, None, script)])
    assert codex_spans.spawning_tool(st, 250) == _id("a")


def test_a_subagent_always_has_a_parent_when_an_exec_had_begun():
    """Nothing says a script spawns (it is built at run time), but the child
    is there: the running call, else the last one begun."""
    st = _agent_after([("a", 100, None, SHELL_SCRIPT), ("b", 120, 140, SHELL_SCRIPT)])
    assert codex_spans.spawning_tool(st, 150) == _id("a")
    done = _agent_after([("a", 100, 120, SHELL_SCRIPT), ("b", 130, 140, SHELL_SCRIPT)])
    assert codex_spans.spawning_tool(done, 150) == _id("b")
    assert codex_spans.spawning_tool(done, 50) is None


def test_calls_that_may_spawn_come_before_calls_that_cannot_when_all_run():
    st = _agent_after([("shell", 100, None, SHELL_SCRIPT),
                       ("a", 200, None, TEMPLATE_SPAWN)])
    assert codex_spans.spawning_tool(st, 250) == _id("a")


def _with_open_spawn(calls, spawn_name="collaboration__spawn_agent"):
    st = _agent_after(calls)
    codex_spans.build([_record(codex_rollout.TOOL_CALL, 300, call_id="direct",
                               name=spawn_name, arguments="{}")], st, _ctx())
    return st


@pytest.mark.parametrize("name", ["collaboration__spawn_agent",
                                  "multi_agent_v1__spawn_agent", "spawn_agent"])
def test_no_guess_is_made_while_a_spawn_call_that_will_name_its_agent_is_open(name):
    """The call's output, or multi_agent_v2's sub_agent_activity beside it,
    names the child exactly; a guess made before it arrives is never
    corrected, because the child's root keeps the parent it was built with."""
    st = _with_open_spawn([("a", 100, None, NO_TOOL_SCRIPT)], name)
    assert codex_spans.spawning_tool(st, 350) is None
    codex_spans.build([_record(codex_rollout.TOOL_OUTPUT, 400, call_id="direct",
                               output="{}")], st, _ctx())
    assert codex_spans.spawning_tool(st, 350) == _id("a")


def test_an_open_exec_call_does_not_make_the_spawner_wait():
    st = _agent_after([("a", 100, None, SPAWN_SCRIPT)])
    assert codex_spans.spawning_tool(st, 350) == _id("a")


def _v2_rollouts(folder, activity_line):
    """A parent whose `exec` call (no tool named) is still running when it
    makes a direct v2 spawn call; the child exists from T0 + 30."""
    folder.mkdir()
    child = _uuid7_at(T0 + 30, "c" * 19)
    spawn = _item(T0 + 10, {
        "type": "function_call", "name": "spawn_agent", "namespace": "collaboration",
        "call_id": "call_direct", "arguments": "{}"})
    started = _event(T0 + 60, {
        "type": "sub_agent_activity", "event_id": "call_direct",
        "agent_thread_id": child, "kind": "started"})
    done = _item(T0 + 70, {"type": "function_call_output", "call_id": "call_direct",
                           "output": '{"task_name":"/root/sub"}'})
    head = [_line(T0 - 100, "session_meta", {"id": PARENT, "cwd": "/tmp/proj",
                                             "cli_version": "0.144.1"}),
            _event(T0, {"type": "task_started", "turn_id": "t1"}),
            _exec_call(T0 + 1, "call_exec", NO_TOOL_SCRIPT), spawn]
    tail = [started, done, _exec_done(T0 + 500, "call_exec"),
            _event(T0 + 900, {"type": "task_complete", "turn_id": "t1",
                              "last_agent_message": ""})]
    path = folder / ("rollout-p-%s.jsonl" % PARENT)
    _rollout(path, head)
    child_path = _rollout(folder / ("rollout-c-%s.jsonl" % child), [
        _line(T0 + 31, "session_meta", {
            "id": child, "cwd": "/tmp/proj", "cli_version": "0.144.1",
            "source": {"subagent": {"thread_spawn": {"parent_thread_id": PARENT}}}})]
        + _turn(T0 + 40, "t-child"))
    return path, child_path, child, head, tail


def test_a_v2_child_read_before_its_activity_line_is_not_put_under_the_exec(
        codex_home, tmp_path, sent):
    parent, child_path, child, head, tail = _v2_rollouts(tmp_path / "sessions", None)
    _run(tmp_path, "UserPromptSubmit", parent)
    _run(tmp_path, "SubagentStop", parent, agent_id=child, agent_type="default",
         agent_transcript_path=str(child_path))
    assert _latest(sent).get(spans.span_id_for("subagent:" + child)) is None
    _rollout(parent, head + tail)
    _run(tmp_path, "Stop", parent)
    root = _latest(sent)[spans.span_id_for("subagent:" + child)]
    assert root.parent_span_id == spans.span_id_for("call_direct")


def test_a_spawn_that_hides_its_name_still_gets_its_subagent_placed(
        codex_home, tmp_path, sent):
    """The reviewer's repro: a template literal spawning next to a tool
    called outside it."""
    folder = tmp_path / "sessions"
    folder.mkdir()
    child = _uuid7_at(T0 + 20, "d" * 19)
    parent = _rollout(folder / ("rollout-p-%s.jsonl" % PARENT), [
        _line(T0 - 100, "session_meta", {"id": PARENT, "cwd": "/tmp/proj",
                                        "cli_version": "0.144.1"})]
        + _turn(T0, "t1", _exec_call(T0 + 1, "call_exec", TEMPLATE_SPAWN),
                _exec_done(T0 + 500, "call_exec")))
    child_path = _rollout(folder / ("rollout-c-%s.jsonl" % child), [
        _line(T0 + 10, "session_meta", {
            "id": child, "cwd": "/tmp/proj", "cli_version": "0.144.1",
            "source": {"subagent": {"thread_spawn": {"parent_thread_id": PARENT}}}})]
        + _turn(T0 + 40, "t-child"))
    _run(tmp_path, "SubagentStop", parent, agent_id=child, agent_type="default",
         agent_transcript_path=str(child_path))
    _run(tmp_path, "Stop", parent)
    root = _latest(sent)[spans.span_id_for("subagent:" + child)]
    assert root.parent_span_id == spans.span_id_for("call_exec")


def test_overlapping_spawns_are_placed_in_the_order_their_hooks_arrive(tmp_path):
    """Known approximation. The children are told apart by when they were
    made, but only among those whose hook has been seen: if the younger
    child's hook comes first it takes the call that began first."""
    from rius_cc import codex_session
    parent, children, first, second = _concurrent_rollouts(tmp_path / "sessions")
    st = codex_session.load({})
    for child in (second, first):
        codex_session.note_payload(
            st, "SubagentStop", {"transcript_path": str(parent), "agent_id": child,
                                 "agent_type": "default",
                                 "agent_transcript_path": str(children[child])})
        codex_session.build(st, _ctx(), str(parent))
    assert st["spawned"][second] == spans.span_id_for("call_a")
    assert st["spawned"][first] == spans.span_id_for("call_b")
