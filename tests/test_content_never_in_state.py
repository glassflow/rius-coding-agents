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
"""
import json
import pathlib

from rius_cc import spans, state, subagents, transcript

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
