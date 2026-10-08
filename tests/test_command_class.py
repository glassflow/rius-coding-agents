"""A shell call says what kind of command it ran, never the command.

With capture off a tool span carried only `gen_ai.tool.name` and its
status, so a session read "Bash failed" where the developer wanted "tests
failed". Each harness now reads the command on this machine, when the call
starts, and sends one word from command_class.CLASSES as
`rius.command.class`, plus `process.exit.code` where the harness reports
one. The tests below hold both halves: the word is right, and nothing of
the command rides along with it.
"""
import json
import random
import string

import pytest

from rius_cc import (codex_rollout, codex_spans, command_class, cursor_events,
                     cursor_spans, spans, transcript)

CLASS = command_class.ATTRIBUTE
EXIT = command_class.EXIT_CODE_ATTRIBUTE


@pytest.mark.parametrize("command, expected", [
    ("pytest -q", "test"),
    ("cd app && npm test", "test"),
    (".venv/bin/python -m pytest -q tests/", "test"),
    ("uv run pytest", "test"),
    ("NODE_ENV=test pnpm vitest run", "test"),
    ("pnpm e2e e2e/x.spec.ts", "test"),
    ("gate make check", "test"),
    ("go test ./...", "test"),
    ("./gradlew test", "test"),
    ("timeout 30 cargo test", "test"),
    ("git add . && pnpm test", "test"),
    ("pnpm build", "build"),
    ("npm run build", "build"),
    ("make", "build"),
    ("go build ./cmd/x", "build"),
    ("docker build -t x .", "build"),
    ("tsc", "build"),
    ("pnpm typecheck", "lint"),
    ("tsc --noEmit", "lint"),
    ("npx eslint .", "lint"),
    ("ruff check scripts", "lint"),
    ("cargo clippy", "lint"),
    ("pnpm install", "package"),
    ("yarn", "package"),
    ("pip install requests", "package"),
    ("sudo -E apt-get install jq", "package"),
    ("go mod tidy", "package"),
    ("git status", "git"),
    ("rtk git log --oneline -3", "git"),
    ("gh pr view 12", "git"),
    ("ls -la", "other"),
    ("echo test", "other"),
    ("grep -rn test src", "other"),
    ("make devstack-up", "other"),
    ("npm run", "other"),
    ("", "other"),
    (None, "other"),
])
def test_a_command_is_read_by_what_it_runs(command, expected):
    assert command_class.classify(command) == expected


def test_the_answer_is_always_one_of_the_classes():
    """Whatever the command, the value is a fixed word: it cannot carry a
    path, an argument or a secret out with it."""
    rng = random.Random(1339)
    alphabet = string.ascii_letters + string.digits + " -_./=&|;'\"$\n"
    commands = ["".join(rng.choice(alphabet) for _ in range(rng.randint(0, 80)))
                for _ in range(2000)]
    commands += ["x" * 100000, "a && " * 5000, "sudo " * 5000]
    for command in commands:
        assert command_class.classify(command) in command_class.CLASSES


def test_a_tool_without_a_command_gets_no_class():
    assert command_class.of_input({"file_path": "/x"}) is None
    assert command_class.of_input('{"command": "  "}') is None
    assert command_class.of_input("not json") is None
    assert command_class.of_input({"command": ["go", "test"]}) == "test"


# Claude Code ------------------------------------------------------------

SECRET_COMMAND = "pnpm test --filter billing-canary-7f3e"


def _cc_line(kind, uuid, ts, message, **extra):
    line = {"isSidechain": False, "sessionId": "s-1339", "cwd": "/tmp/proj",
            "gitBranch": "main", "version": "2.1.284", "type": kind,
            "uuid": uuid, "parentUuid": None,
            "timestamp": "2026-10-08T10:00:%02d.000Z" % ts, "message": message}
    line.update(extra)
    return json.dumps(line)


def _cc_spans(tmp_path, result, capture=False):
    tool_use = {"type": "tool_use", "id": "toolu_t", "name": "Bash",
                "input": {"command": SECRET_COMMAND}}
    # Not a shell tool, though its input has a `command`: an MCP server's.
    mcp = {"type": "tool_use", "id": "toolu_m", "name": "mcp__ops__run",
           "input": {"command": "pnpm build"}}
    lines = [
        _cc_line("user", "u0", 0, {"role": "user", "content": "run the tests"},
                 promptId="p1"),
        _cc_line("assistant", "a1", 1, {
            "id": "m1", "role": "assistant", "model": "claude-opus-5",
            "stop_reason": "tool_use", "content": [tool_use, mcp],
            "usage": {"input_tokens": 2, "output_tokens": 9}}),
    ]
    if result is not None:
        lines.append(_cc_line("user", "r1", 2, {"role": "user", "content": [
            dict(result, type="tool_result", tool_use_id="toolu_t"),
            {"type": "tool_result", "tool_use_id": "toolu_m", "content": "x"}]},
            promptId="p1"))
    path = tmp_path / "t.jsonl"
    path.write_text("\n".join(lines) + "\n")
    entries, _ = transcript.read_from(str(path), 0)
    ctx = spans.Ctx(session_id="s-1339", cwd="/tmp/proj", git_branch="main",
                    cc_version="2.1.284", service_name="claude-code",
                    capture_content=capture, max_attr_bytes=32768)
    state = {"offset": 0, "open_tools": {}, "open_turns": {},
             "root_started": False, "root_start_ns": 0, "last_ns": 0,
             "open_task_spans": []}
    out = spans.build(entries, state, ctx)
    return out, state


def _tool(out, tool_id, pending=False):
    return next(s for s in out if s.span_id == spans.span_id_for(tool_id)
                and s.pending == pending)


def test_claude_code_names_a_failed_test_run_with_capture_off(tmp_path):
    out, _ = _cc_spans(tmp_path, {"is_error": True,
                                  "content": "Exit code 1\n2 tests failed"})
    span = _tool(out, "toolu_t")
    assert span.attributes[CLASS] == "test"
    assert span.attributes[EXIT] == 1
    assert span.attributes["error.type"] == "Bash.exit_1"
    # Only Bash is read as a shell command.
    assert CLASS not in _tool(out, "toolu_m").attributes


def test_claude_code_sends_no_exit_code_it_was_not_given(tmp_path):
    """A Bash call that succeeded reports no exit code: none is invented."""
    out, _ = _cc_spans(tmp_path, {"content": "12 passed"})
    span = _tool(out, "toolu_t")
    assert span.attributes[CLASS] == "test"
    assert EXIT not in span.attributes


def test_claude_code_says_what_a_running_call_is_doing(tmp_path):
    """The pending span passes an allowlist; the class is on it."""
    out, state = _cc_spans(tmp_path, None)
    assert _tool(out, "toolu_t", pending=True).attributes[CLASS] == "test"
    # Interrupted: the session closes the call with the class it had.
    closed = spans._close_open_tools(state, spans.Ctx(
        "s-1339", "/tmp/proj", "main", "2.1.284", "claude-code", False, 32768),
        "t", 10)
    assert {s.attributes.get(CLASS) for s in closed} == {"test", None}


def test_claude_code_keeps_no_command_anywhere(tmp_path):
    for result in (None, {"is_error": True, "content": "Exit code 2\nboom"}):
        out, state = _cc_spans(tmp_path, result)
        assert "canary-7f3e" not in json.dumps(state)
        for span in out:
            assert "canary-7f3e" not in json.dumps(span.attributes)
            assert "canary-7f3e" not in span.status_message


# Codex ------------------------------------------------------------------

def _codex_tool(tmp_path, name, arguments, output, capture=False):
    def line(ms, kind, payload):
        return json.dumps({"timestamp": "2026-10-08T10:00:00.%03dZ" % ms,
                           "type": kind, "payload": payload})
    lines = [line(1, "event_msg", {"type": "task_started", "turn_id": "t1"}),
             line(2, "response_item", {"type": "function_call", "call_id": "c1",
                                       "name": name, "arguments": arguments}),
             line(4, "response_item", {"type": "function_call_output",
                                       "call_id": "c1", "output": output})]
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text("\n".join(lines) + "\n")
    records, _ = codex_rollout.read_from(str(rollout), 0)
    ctx = spans.Ctx("s1", "/x", "", "", "codex", capture, 32768)
    state = codex_spans.new_state()
    out = codex_spans.build(records, state, ctx)
    tools = [s for s in out if s.kind_oi == "TOOL"]
    return ({s.pending: s for s in tools}, state)


def test_codex_names_a_command_and_its_exit_code(tmp_path):
    tools, state = _codex_tool(
        tmp_path, "exec_command", json.dumps({"cmd": SECRET_COMMAND}),
        "Process exited with code 1\nOutput:\nFAIL\n")
    assert tools[True].attributes[CLASS] == "test"
    assert tools[False].attributes[CLASS] == "test"
    assert tools[False].attributes[EXIT] == 1
    assert tools[False].attributes["error.type"] == "exec_command.exit_1"
    assert "canary-7f3e" not in json.dumps(state)
    assert "canary-7f3e" not in json.dumps([t.attributes for t in tools.values()])


def test_codex_reports_a_command_that_succeeded_as_exit_0(tmp_path):
    tools, _ = _codex_tool(
        tmp_path, "exec_command", json.dumps({"cmd": "git status"}),
        "Process exited with code 0\nOutput:\nclean\n")
    assert tools[False].attributes[CLASS] == "git"
    assert tools[False].attributes[EXIT] == 0
    assert tools[False].status_code == "OK"


def test_codex_reads_the_commands_a_code_mode_script_runs(tmp_path):
    script = 'const r = await tools.exec_command({cmd: "cargo build"});\nr'
    tools, _ = _codex_tool(tmp_path, "exec", script, "ok")
    assert tools[False].attributes[CLASS] == "build"


def test_codex_gives_no_class_to_a_call_that_runs_no_command(tmp_path):
    tools, _ = _codex_tool(tmp_path, "view_image", '{"path": "/x.png"}', "ok")
    assert CLASS not in tools[False].attributes
    assert EXIT not in tools[False].attributes


# Cursor -----------------------------------------------------------------

def _cursor_spans(exit_code, capture=False):
    base = {"conversation_id": "conv-1339", "generation_id": "gen-1",
            "tool_name": "Shell", "tool_use_id": "call-1",
            "tool_input": {"command": SECRET_COMMAND, "cwd": "", "timeout": 30000}}
    payloads = [
        dict(base, hook_event_name="beforeSubmitPrompt", prompt="run tests"),
        dict(base, hook_event_name="preToolUse"),
        dict(base, hook_event_name="postToolUse",
             tool_output=json.dumps({"output": "FAIL", "exitCode": exit_code})),
        dict(base, hook_event_name="stop", status="completed"),
    ]
    records = [cursor_events.to_record(p, 1790000000000000000 + i * 1000000,
                                       capture, 32768)
               for i, p in enumerate(payloads)]
    ctx = cursor_spans.Ctx("conv-1339", capture_content=capture,
                           max_attr_bytes=32768)
    return records, cursor_spans.build(records, ctx)


def test_cursor_names_a_command_and_its_exit_code():
    records, out = _cursor_spans(1)
    shell = next(s for s in out if s.name == "Shell")
    assert shell.attributes[CLASS] == "test"
    assert shell.attributes[EXIT] == 1
    assert shell.attributes["error.type"] == "Shell.exit_1"
    # With capture off the spool holds the word, not the command.
    assert "canary-7f3e" not in json.dumps(records)
    assert {r.get("command_class") for r in records} >= {"test"}


def test_cursor_sends_exit_0_for_a_command_that_succeeded():
    _, out = _cursor_spans(0)
    shell = next(s for s in out if s.name == "Shell")
    assert shell.attributes[EXIT] == 0
    assert shell.status_code == "OK"


def test_cursor_ignores_a_spooled_class_that_is_not_a_class():
    """The spool is a file on disk: a value it holds is checked again."""
    records, _ = _cursor_spans(0)
    for r in records:
        if "command_class" in r:
            r["command_class"] = "pnpm test --filter secret"
    out = cursor_spans.build(records, cursor_spans.Ctx(
        "conv-1339", capture_content=False, max_attr_bytes=32768))
    shell = next(s for s in out if s.name == "Shell")
    assert CLASS not in shell.attributes


def test_a_runner_that_runs_a_runner_has_a_floor():
    """`npm exec` and `bun x` step into the command they run; a chain of
    them stops after a few steps instead of exhausting the stack."""
    assert command_class.classify("pnpm dlx bun x vitest") == "test"
    for chain in ("bun x " * 700, "npm --yes exec " * 300, "yarn dlx " * 500):
        assert command_class.classify(chain + "vitest") == "other"


def test_an_exit_code_too_long_to_be_one_is_not_read():
    """int() of a 4300-digit run raises on Python 3.11+: the hook must not."""
    digits = "9" * 5000
    assert spans.exit_code_of("Exit code %s\nboom" % digits) is None
    assert spans.exit_code_of("Exit code 137\nkilled") == 137
    assert codex_spans.exit_code_of(
        "Process exited with code %s\nOutput:\n" % digits) is None
    assert codex_spans.exit_code_of("Process exited with code -1\nOutput:\n") == -1
    assert codex_spans.tool_error({"name": "exec_command"},
                                  "Process exited with code -%s\n" % digits) is None
