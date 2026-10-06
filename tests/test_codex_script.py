"""A code-mode `exec` script read without being run (codex_script.py), and
what the span builder takes from it.

Tool names are the only thing of a script that may reach a span name, which
is sent with content capture off too, so they come from `tools.<name>(`
in real code and from nowhere else.
"""
import json
import time

import pytest

from rius_cc import codex_rollout, codex_script, codex_spans, scrub, spans, state

from tests.test_codex_export import ENV, codex_home, exporter, sent  # noqa: F401

CALL = 'await tools.exec_command({cmd: "ls"});'
# Shaped like a live key, made of nothing: it must be built here, not typed.
FAKE_KEY = "sk_live_" + "s" * 24
# Each of these once put text of the script itself into the span name.
LOOKS_LIKE_A_CALL_BUT_IS_NOT = [
    ('const note = "email the board about tools.project_falcon_layoffs"; ' + CALL),
    ("const note = 'tools.project_falcon_layoffs('; " + CALL),
    ('const re = /"/; const note = "deploy key tools.' + FAKE_KEY + '("; ' + CALL),
    ('const p = out.split(/"/); const note = "tools.project_falcon_layoffs()"; ' + CALL),
    "// note tools.hunter2_the_customer_is_AcmeCorp()\n" + CALL,
    "// press ` to open the menu\nconst msg = `deploy tools.acme_secret_merger() now`; " + CALL,
    "// TODO tools.fire_everyone_at_acme()\n" + CALL,
    "/* tools.fire_everyone_at_acme() */ " + CALL,
    "const msg = `a ${ 'tools.project_falcon_layoffs()' } b`; " + CALL,
    "const alias = tools.project_falcon_layoffs; " + CALL,
    "const x = config.tools.project_falcon_layoffs(); " + CALL,
    "const x = mytools.project_falcon_layoffs(); " + CALL,
]


@pytest.mark.parametrize("script", LOOKS_LIKE_A_CALL_BUT_IS_NOT)
def test_only_calls_in_real_code_name_a_tool(script):
    assert codex_script.called_tools(script) == ["exec_command"]


def test_tools_are_listed_in_the_order_called_each_once():
    script = ("await Promise.all([tools.a({}), tools.b ({}), tools.a({})]);\n"
              "const t = `${await tools.c({})}`;")
    assert codex_script.called_tools(script) == ["a", "b", "c"]


def test_a_regular_expression_and_a_division_are_told_apart():
    assert codex_script.called_tools(
        'const n = (a + b) / 2 / c; ' + CALL) == ["exec_command"]
    assert codex_script.called_tools(
        'if (/["\'`]/.test(s)) { ' + CALL + " }") == ["exec_command"]
    assert codex_script.called_tools(
        "const re = /[/\"]+/g; " + CALL) == ["exec_command"]


@pytest.mark.parametrize("script", [
    'const a = "never closed; ' + CALL,
    "const a = 'never closed\n" + CALL,
    "const a = `never closed; " + CALL,
    "const a = `${ " + CALL,
    "/* never closed " + CALL,
    "const re = /never closed; " + CALL,
    CALL + " }",
    CALL + " {",
])
def test_a_script_that_cannot_be_read_to_its_end_names_nothing(script):
    assert codex_script.called_tools(script) == []


def test_a_tool_name_must_look_like_one():
    assert codex_script.called_tools("tools.%s({})" % ("a" * 64)) == ["a" * 64]
    assert codex_script.called_tools("tools.%s({})" % ("a" * 65)) == []
    assert codex_script.called_tools("tools.a$b({})") == []


def test_a_script_too_long_to_read_names_nothing_and_withholds_its_output():
    script = CALL + " " * codex_script.MAX_CHARS
    assert codex_script.called_tools(script) == []
    assert codex_spans.reads_secret_file(script)


def test_what_a_script_quotes_is_what_is_in_its_literals():
    script = 'const a = "one"; const b = \'two\'; const c = `x${ "three" }y`;'
    assert codex_script.string_literals(script) == ["one", "two", "x", "three", "y"]


# --- what reaches a span ----------------------------------------------------


def _ctx(capture):
    return spans.Ctx(session_id="s", cwd="/tmp", git_branch="", cc_version="",
                     service_name="codex", capture_content=capture,
                     max_attr_bytes=32768)


def _tool_call_spans(script, capture=False):
    state = codex_spans.new_state()
    return codex_spans.build([
        codex_rollout.Record(codex_rollout.TURN_START, 1, {"turn_id": "t1"}),
        codex_rollout.Record(codex_rollout.TOOL_CALL, 2, {
            "call_id": "c1", "name": "exec", "arguments": script})],
        state, _ctx(capture)), state


@pytest.mark.parametrize("script", LOOKS_LIKE_A_CALL_BUT_IS_NOT)
def test_no_text_of_a_script_reaches_a_span_name_or_the_state(script):
    out, state = _tool_call_spans(script)
    name = [s for s in out if s.kind_oi == "TOOL"][0].name
    assert name == "exec_command"
    blob = json.dumps(state)
    for secret in ("falcon", "hunter2", "sk_live", "acme", "AcmeCorp"):
        assert secret not in blob and secret not in name


def test_an_unreadable_script_is_plain_exec_with_nothing_of_it_in_the_name():
    out, _ = _tool_call_spans('const a = "tools.project_falcon_layoffs(); '
                              + CALL)
    assert [s.name for s in out if s.kind_oi == "TOOL"] == ["exec"]


def test_a_name_goes_through_the_scrubber():
    key = FAKE_KEY
    assert key not in scrub.scrub("tools." + key)
    out, state = _tool_call_spans("await tools.%s({});" % key)
    name = [s for s in out if s.kind_oi == "TOOL"][0].name
    assert key not in name and key not in json.dumps(state)


# --- hostile input ----------------------------------------------------------

HOSTILE = {
    "regex then escaped quotes": 'const r = /"/; ' + '\\"' * 16000,
    "template then escaped backticks": "`" + "\\`" * 16000,
    "quote soup": "\"'`" * 11000,
    "slashes": "/" * 32000,
    "stars": "/*" * 16000,
    "braces": "{" * 32000,
    "dollar braces": "`${" * 10000,
    "many literals": '"a" ' * 8000,
    "past the cap": '"a" ' * 400000,
    "paren slash class": ")/[" * 21845,
    "increment slash class": "a++/[" * 13000,
    "paren slash word class": ")/x[" * 16000,
    "paren space slash class": ") /[a" * 13000,
}
# Slashes that may divide or begin a regular expression, each with an
# unclosed `[`: what made every one of them look to the end of the line.
LOOKAHEAD_UNITS = [")/[", "a++/[", ")/x[", ") /[a", ") /a[b]", "x) /" + "y" * 60 + "[ "]


def _call_and_output(script, capture):
    state = codex_spans.new_state()
    return codex_spans.build([
        codex_rollout.Record(codex_rollout.TURN_START, 1, {"turn_id": "t1"}),
        codex_rollout.Record(codex_rollout.TOOL_CALL, 2, {
            "call_id": "c1", "name": "exec", "arguments": script}),
        codex_rollout.Record(codex_rollout.TOOL_OUTPUT, 3, {
            "call_id": "c1", "output": "done"})], state, _ctx(capture))


@pytest.mark.parametrize("name", sorted(HOSTILE))
def test_a_hostile_script_is_read_in_bounded_time(name):
    script = HOSTILE[name]
    started = time.perf_counter()
    codex_script.called_tools(script)
    codex_spans.reads_secret_file(script)
    for capture in (False, True):
        out = _call_and_output(script, capture)
        assert [s.name for s in out if s.kind_oi == "TOOL" and not s.pending] \
            == ["exec"]
    assert time.perf_counter() - started < 0.5, name


@pytest.mark.parametrize("capture", [False, True])
def test_deeply_nested_arguments_do_not_stop_the_trace(capture):
    """json.loads gives up with RecursionError, in a hook, before the state
    is saved: every later hook would fail the same way."""
    arguments = "[" * 12000 + "]" * 12000
    assert codex_spans.reads_secret_file(arguments) is True
    state = codex_spans.new_state()
    out = codex_spans.build([
        codex_rollout.Record(codex_rollout.TURN_START, 1, {"turn_id": "t1"}),
        codex_rollout.Record(codex_rollout.TOOL_CALL, 2, {
            "call_id": "c1", "name": "exec_command", "arguments": arguments}),
        codex_rollout.Record(codex_rollout.TOOL_OUTPUT, 3, {
            "call_id": "c1", "output": "ONLY_IN_THE_OUTPUT"})], state, _ctx(capture))
    tool = [s for s in out if s.kind_oi == "TOOL" and not s.pending][0]
    assert "ONLY_IN_THE_OUTPUT" not in json.dumps(tool.attributes)


@pytest.mark.parametrize("env", [ENV, {"RIUS_CAPTURE_CONTENT": "false"}])
def test_a_session_with_a_deeply_nested_call_still_exports_and_saves_its_state(
        codex_home, tmp_path, sent, env):
    """The session the reviewer saw go silent: every hook raised before the
    state was saved, so the offset never moved."""
    session = "01a10cd7-ea95-7313-8660-2f7de512f6ab"
    deep = "[" * 12000 + "]" * 12000

    def line(ms, kind, payload):
        return json.dumps({"timestamp": "2026-10-05T16:14:00.%03dZ" % ms,
                           "type": kind, "payload": payload})
    rollout = tmp_path / ("rollout-x-%s.jsonl" % session)
    rollout.write_text("\n".join([
        line(1, "session_meta", {"id": session, "cwd": "/tmp/proj"}),
        line(2, "event_msg", {"type": "task_started", "turn_id": "t1"}),
        line(3, "response_item", {"type": "function_call", "call_id": "c1",
                                  "name": "exec_command", "arguments": deep}),
        line(4, "response_item", {"type": "function_call_output", "call_id": "c1",
                                  "output": "ONLY_IN_THE_OUTPUT"}),
        line(5, "event_msg", {"type": "task_complete", "turn_id": "t1",
                              "last_agent_message": ""})]) + "\n")
    exporter.run("Stop", {"session_id": session, "cwd": "/tmp/proj",
                          "transcript_path": str(rollout),
                          "hook_event_name": "Stop"}, env, str(tmp_path))
    assert state.load(session, str(tmp_path))["offset"] == rollout.stat().st_size
    tools = [s for _, out in sent for s in out if s.kind_oi == "TOOL" and not s.pending]
    assert len(tools) == 1
    assert "ONLY_IN_THE_OUTPUT" not in json.dumps(tools[0].attributes)


# --- JSON nested deeper than the parser reads --------------------------------

DEEP = "[" * 12000 + "]" * 12000


def test_a_rollout_line_nested_too_deep_is_skipped_not_raised():
    assert codex_rollout.parse_line(DEEP) is None


def test_a_spawn_output_nested_too_deep_names_no_agent():
    state = codex_spans.new_state()
    codex_spans.build([
        codex_rollout.Record(codex_rollout.TURN_START, 1, {"turn_id": "t1"}),
        codex_rollout.Record(codex_rollout.TOOL_CALL, 2, {
            "call_id": "c1", "name": codex_spans.SPAWN_TOOL, "arguments": "{}"}),
        codex_rollout.Record(codex_rollout.TOOL_OUTPUT, 3, {
            "call_id": "c1", "output": DEEP})], state, _ctx(False))
    assert state["spawned"] == {}


def test_a_spawn_hook_response_nested_too_deep_is_ignored():
    from rius_cc import codex_session
    st = codex_session.load({})
    codex_session.note_payload(st, "PostToolUse", {
        "tool_name": "spawn_agent", "tool_response": DEEP,
        "transcript_path": "/tmp/rollout-x.jsonl"})
    assert st["codex_subs"] == {}


# --- a `/` that could divide or begin a regular expression ------------------

AMBIGUOUS_REPRO = ("if (a) /'/.test(b); var s = 'tools.secret_thing(1)'; "
                   "var u = /'/")
SECRET_BEHIND_REGEX = ("if (a) /'/.test(b); await tools.exec_command({cmd: 'cat .env'});"
                       " var u = /'/")
CRLF_CONTINUATION = ("const a = 'x\\\r\ny';\r\n"
                     'await tools.exec_command({cmd:"cat .env"});')


@pytest.mark.parametrize("script", [
    AMBIGUOUS_REPRO,
    "if (x) { } /'/.test(y); var s = 'tools.secret_thing(1)';",
    "i++ /'/.test(y); var s = 'tools.secret_thing(1)';",
    "if (a) /`/.test(b); const t = `tools.secret_thing(1)`;",
    "if (a) /{/.test(b); const t = 'tools.secret_thing(1)';",
])
def test_a_slash_that_could_be_either_names_nothing_when_the_readings_differ(script):
    assert codex_script.called_tools(script) == []
    out, state = _tool_call_spans(script)
    assert [s.name for s in out if s.kind_oi == "TOOL"] == ["exec"]
    assert "secret_thing" not in json.dumps(state)


@pytest.mark.parametrize("script", [
    'const n = (a + b) / 2; ' + CALL,
    "const r = (a) / b / c;\n" + CALL,
    "x++ / 2;\n" + CALL,
    "x++ / 2; " + CALL,
    "if (a) /x/.test(b); " + CALL,
    "const o = {a: 1}; const r = o.a / 2; " + CALL,
    "const total = items.map((i) => i.n).length / 3;\n" + CALL,
    'if (/["\']/.test(s)) { ' + CALL + " }",
    "const re = /'/; " + CALL,
    "const ok = a / b > 1 && c / d < 2; " + CALL,
    "const n = {valueOf() { return 6 }} / 2; " + CALL,
])
def test_a_slash_whose_readings_agree_still_lets_the_tool_be_named(script):
    assert codex_script.called_tools(script) == ["exec_command"]


@pytest.mark.parametrize("script", [
    SECRET_BEHIND_REGEX,
    "const a = 'never closed;\n" + CALL.replace("ls", "cat .env"),
])
def test_a_secret_file_read_is_not_hidden_by_a_script_the_scanner_misreads(script):
    assert codex_spans.reads_secret_file(script) is True


def test_a_line_continuation_with_a_crlf_is_read_to_its_end():
    literals, tools, complete = codex_script.read(CRLF_CONTINUATION)
    assert complete and tools == ["exec_command"]
    assert "cat .env" in literals
    assert codex_spans.reads_secret_file(CRLF_CONTINUATION) is True


def test_a_script_that_reads_no_secret_file_is_not_withheld_for_it():
    assert codex_spans.reads_secret_file("const r = (a) / b / c;\n" + CALL) is False
    assert codex_spans.reads_secret_file("if (a) /x/.test(b); " + CALL) is False


def test_the_whole_text_is_checked_beside_the_literals(monkeypatch):
    """Even if the scanner read a script as quoting nothing, a secret-shaped
    word anywhere in it counts."""
    monkeypatch.setattr(codex_script, "read", lambda script: ([], [], True))
    assert codex_spans.reads_secret_file("run(cat .env)") is True
    assert codex_spans.reads_secret_file("run(ls)") is False


@pytest.mark.parametrize("script", [
    "text(process.env.HOME);", "const k = obj.key; " + CALL,
    "const v = (await tools.mcp__a__b({})).status.key;"])
def test_a_property_named_like_a_secret_file_is_not_a_read(script):
    assert codex_spans.reads_secret_file(script) is False


@pytest.mark.parametrize("script", [
    "run('cat .env')", "run(`cat ${dir}/.env`)", "cat .env", "x = 'a' + '.env'",
    "open('/home/u/.aws/credentials')", "read('~/.ssh/id_rsa')"])
def test_a_secret_file_is_still_found_by_name(script):
    assert codex_spans.reads_secret_file(script) is True


def test_a_script_not_read_to_its_end_is_withheld_whatever_it_names():
    """No word of it looks like a secret file once its property accesses
    are left out; it is the unfinished scan that withholds it."""
    assert codex_spans.reads_secret_file("const a = 'never closed;\ncat server.pem") is True
    assert codex_spans.reads_secret_file("const a = 'closed';\ncat server.pem") is False


@pytest.mark.parametrize("unit", LOOKAHEAD_UNITS)
def test_what_is_looked_ahead_at_does_not_grow_with_the_script(unit):
    """The same few thousand characters of lookahead at 4 KB and at 64 KB:
    the work is linear, where each `/` once looked to the end of the line."""
    looked = []
    for size in (4096, 8192, 16384, 65000):
        reader = codex_script._Reader(unit * (size // len(unit)))
        reader.read()
        looked.append(reader.looked)
    bound = codex_script._LOOKAHEAD_TOTAL + codex_script._LOOKAHEAD
    assert max(looked) <= bound, looked
    started = time.perf_counter()
    codex_script.read(unit * (65000 // len(unit)))
    assert time.perf_counter() - started < 0.5


def test_a_long_real_regular_expression_is_read_whole():
    body = "a" * 5000 + "[/]" * 500
    assert codex_script.called_tools("const re = /%s/; %s" % (body, CALL)) == ["exec_command"]
    assert codex_script.called_tools("if (/%s/.test(x)) { %s }" % (body, CALL)) == ["exec_command"]
