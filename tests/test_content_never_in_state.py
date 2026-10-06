"""Systemic guard: with capture off, no content may reach the state file.

`state.save` writes the builder's whole dict to
`~/.claude/rius/state/<sessionId>.json`, in plaintext, on EVERY hook event
and for as long as the session lives. Three separate defects in this family
have shipped already -- the turn text (I6), the cached subagent brief, and
`open_tools["input_json"]` -- because each was fixed at the one place it was
noticed. This test is not aimed at any of them.

It is driven off the fixtures themselves: it pulls every content string out
of each fixture transcript, replays it through the real builder with
`capture_content=False`, and asserts none of those strings appears anywhere
in the state -- checked after every single entry (mid-flight, the window a
long-running tool or subagent spends its whole life in), not only at the end.
A fixture added later is covered the day it lands, with no list to update.

Codex has the same guard below, over its rollouts, and holds it to more: its
builder keeps no content in the state with capture ON either, only the place
in the rollout to read it from again when a span is finished.
"""
import json
import pathlib

import pytest

from rius_cc import (codex_rollout, codex_spans, spans, state, subagents,
                     transcript)
from tests.test_codex_export import (CHILD, PARENT, codex_home,  # noqa: F401
                                     exporter, rollouts, sent)

# Short strings collide with structural values by accident ("Read", "main",
# a model name). Content worth protecting is longer than this, and the cost
# of the floor is only that a very short prompt goes unguarded.
MIN_CONTENT_LEN = 12


def _add(out, value):
    if isinstance(value, str) and len(value.strip()) >= MIN_CONTENT_LEN:
        out.add(value.strip())


def content_strings(path: pathlib.Path) -> set:
    """Every string in one transcript that counts as content under §7:
    prompt and message text, tool inputs, tool results."""
    out = set()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return out
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except ValueError:
            continue
        if not isinstance(raw, dict):
            continue
        message = raw.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, str):
            _add(out, content)
            continue
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "text":
                _add(out, block.get("text"))
            elif kind == "tool_use":
                inp = block.get("input")
                if isinstance(inp, dict):
                    _add(out, json.dumps(inp))
                    for value in inp.values():
                        _add(out, value)
            elif kind == "tool_result":
                body = block.get("content")
                _add(out, body if isinstance(body, str) else json.dumps(body))
    return out


def _transcripts(fixtures_dir):
    """Every fixture transcript, including the per-subagent ones."""
    return sorted(p for p in fixtures_dir.rglob("*.jsonl") if p.stat().st_size)


def _expected_content(main_path: pathlib.Path) -> set:
    """This transcript's content, plus that of any subagent it spawns --
    a subagent's brief and tool output are this session's content too."""
    out = content_strings(main_path)
    subdir = pathlib.Path(subagents.dir_for(str(main_path), ""))
    if subdir.is_dir():
        for sub in sorted(subdir.glob("*.jsonl")):
            out |= content_strings(sub)
    return out


def test_no_fixture_content_ever_reaches_state_with_capture_off(fixtures_dir):
    paths = _transcripts(fixtures_dir)
    assert paths, "no fixture transcripts found -- this guard would be vacuous"

    checked = 0
    for path in paths:
        entries, _ = transcript.read_from(str(path), 0)
        if not entries:
            continue
        secrets = _expected_content(path)
        if not secrets:
            continue
        session_id = entries[0].session_id or "guard-session"
        st = state.new_state()
        ctx = spans.Ctx(session_id=session_id, cwd="/tmp/proj",
                        git_branch="main", cc_version="2.1.278",
                        service_name="claude-code", capture_content=False,
                        max_attr_bytes=32768)
        subdir = subagents.dir_for(str(path), session_id)

        def _check(when):
            blob = json.dumps(st)
            for secret in secrets:
                assert secret not in blob, (
                    "%s: content from %s is in the state file %s -- it will be "
                    "written to ~/.claude/rius/state/ in plaintext on every "
                    "hook event" % (path.name, path.name, when))

        # One entry at a time: the real hook cadence, and the only way to see
        # the mid-flight window an open tool or a running subagent lives in.
        for i in range(len(entries)):
            spans.build([entries[i]], st, ctx)
            subagents.expand(st, ctx, subdir)
            _check("mid-flight after entry %d" % i)

        now_ns = (st.get("last_ns") or 0) + 1_000_000_000
        subagents.finalize(st, ctx, subdir, now_ns)
        spans.finalize_session(st, ctx, now_ns)
        _check("at completion")
        checked += 1

    assert checked >= 6, "expected every fixture transcript to be exercised"


def test_the_guard_can_actually_fail(fixtures_dir):
    """A guard that cannot fail is worse than none: it reads as coverage.
    This is the shape of the leak it is watching for."""
    path = fixtures_dir / "subagent_files" / \
        "55555555-5555-5555-5555-555555555555.jsonl"
    secrets = _expected_content(path)
    assert "explore the fixture tree" in secrets
    assert any("subagent_type" in s for s in secrets)
    leaky = json.dumps({"open_tools": {"toolu_agent1": {
        "input_json": '{"prompt": "explore the fixture tree"}'}}})
    assert any(s in leaky for s in secrets)


# Codex rollouts are short and their prompts shorter ("reply 8").
CODEX_MIN_CONTENT_LEN = 6


def _codex_texts(payload):
    """The content strings of one rollout line's payload."""
    kind = payload.get("type")
    if kind in ("user_message", "agent_message"):
        yield payload.get("message")
    elif kind == "task_complete":
        yield payload.get("last_agent_message")
    elif kind == "function_call":
        yield payload.get("arguments")
    elif kind == "custom_tool_call":
        yield payload.get("input")
    elif kind == "mcp_tool_call_end":
        result = payload.get("result") or {}
        yield result.get("Err")
        for part in (result.get("Ok") or {}).get("content") or []:
            yield part.get("text")
    elif kind in ("function_call_output", "custom_tool_call_output"):
        output = payload.get("output")
        for part in output if isinstance(output, list) else [output]:
            yield part.get("text") if isinstance(part, dict) else part


def codex_content_strings(path: pathlib.Path) -> set:
    """Every string in one Codex rollout that counts as content: prompts and
    replies, tool inputs and outputs, MCP errors."""
    out = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        payload = json.loads(line).get("payload")
        for text in _codex_texts(payload if isinstance(payload, dict) else {}):
            if isinstance(text, str) and len(text.strip()) >= CODEX_MIN_CONTENT_LEN:
                out.add(text.strip())
    return out


def _codex_ctx(capture):
    return spans.Ctx(session_id="guard-session", cwd="/tmp/proj", git_branch="",
                     cc_version="", service_name="codex",
                     capture_content=capture, max_attr_bytes=32768)


def _replay_a_growing_rollout(path, live, ctx, check):
    """The rollout, a line at a time into `live`, built after each as a hook
    reads a rollout Codex is still writing. Every span built."""
    st, offset, built = codex_spans.new_state(), 0, []
    live.write_bytes(b"")
    for line in path.read_bytes().splitlines(keepends=True):
        with open(str(live), "ab") as fh:
            fh.write(line)
        records, offset = codex_rollout.read_from(str(live), offset)
        built += codex_spans.build(records, st, ctx)
        check(st, "mid-flight, after %r" % line[:60])
    built += codex_spans.finalize_session(st, ctx, 2 * 10**18)
    check(st, "at completion")
    return built


def _codex_rollouts(fixtures_dir):
    paths = sorted((fixtures_dir / "codex").rglob("*.jsonl"))
    return [p for p in paths if p.stat().st_size]


def _exported_text(built):
    return json.dumps([s.attributes for s in built if not s.pending])


def _codex_prompts(path):
    payloads = (json.loads(line)["payload"] for line in path.read_text().splitlines())
    return {p["message"].strip() for p in payloads
            if p.get("type") == "user_message"
            and len(p["message"].strip()) >= CODEX_MIN_CONTENT_LEN}


@pytest.mark.parametrize("capture", [False, True])
def test_no_codex_content_ever_reaches_state(fixtures_dir, tmp_path, capture):
    """With capture off there is none to keep; with it on the finished spans
    still carry it, read back from the rollout, but the state never holds it."""
    paths = _codex_rollouts(fixtures_dir)
    assert len(paths) >= 6, "this guard would be vacuous"
    sent_prompts = set()
    for path in paths:
        secrets = codex_content_strings(path)

        def check(st, when, path=path, secrets=secrets):
            blob = json.dumps(st)
            for secret in secrets:
                assert secret not in blob, (
                    "%s: %r is in the state file %s -- it will be written to "
                    "~/.codex/rius/state/ in plaintext on every hook event"
                    % (path.name, secret, when))
        built = _replay_a_growing_rollout(path, tmp_path / "live.jsonl",
                                          _codex_ctx(capture), check)
        exported = _exported_text(built)
        sent_prompts |= {p for p in _codex_prompts(path) if p in exported}
    assert bool(sent_prompts) == capture
    assert not capture or len(sent_prompts) >= 4, "capture on sent too little"


def test_the_codex_guard_can_actually_fail(fixtures_dir):
    path = fixtures_dir / "codex" / "mock_tools_mcp_resume.jsonl"
    secrets = codex_content_strings(path)
    assert "tools please" in secrets
    assert any("nonexistent-rius-t3" in s for s in secrets)
    leaky = json.dumps({"turn": {"text": "tools please"}})
    assert any(s in leaky for s in secrets)


@pytest.mark.parametrize("capture", [False, True])
def test_the_codex_state_file_holds_no_content_between_hooks(
        codex_home, tmp_path, sent, rollouts, capture):
    """What `state.save` wrote after each hook of a session with a subagent,
    the rollout growing a line at a time: the parent's state and the child's
    alike, mid-turn and with a tool open."""
    secrets = codex_content_strings(rollouts[PARENT]) | codex_content_strings(
        rollouts[CHILD])
    env = {} if capture else {"RIUS_CAPTURE_CONTENT": "false"}
    lines = rollouts[PARENT].read_bytes().splitlines(keepends=True)
    folder = tmp_path / "growing"
    folder.mkdir()
    growing = folder / rollouts[PARENT].name
    child = folder / rollouts[CHILD].name
    child.write_bytes(rollouts[CHILD].read_bytes())

    def hook(event, rollout, **extra):
        payload = {"session_id": PARENT, "cwd": "/tmp/proj",
                   "hook_event_name": event, "transcript_path": str(rollout)}
        payload.update(extra)
        exporter.run(event, payload, env, str(tmp_path))
        blob = json.dumps(state.load(PARENT, str(tmp_path)))
        assert [s for s in secrets if s in blob] == [], (event, len(lines))
    for cut in range(1, len(lines) + 1):
        growing.write_bytes(b"".join(lines[:cut]))
        hook("PostToolUse", growing)
        if cut == len(lines) // 2:
            hook("PostToolUse", child, agent_id=CHILD, agent_type="default",
                 tool_name="Bash")
    hook("SubagentStop", growing, agent_id=CHILD, agent_type="default",
         agent_transcript_path=str(child))
    hook("Stop", growing)
    sent_text = json.dumps([s.attributes for _, out in sent for s in out
                            if not s.pending])
    assert ("tools please" in sent_text) == capture
    assert ("from-sub" in sent_text) == capture
