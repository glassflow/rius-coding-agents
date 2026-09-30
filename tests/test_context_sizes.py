"""Every generation says what its prompt was made of: `rius.context.sizes`.

The console's Context panel splits a call's prompt into buckets (user and
assistant history, the current turn, each tool's calls and results). Without
content or this attribute it can only show the provider's cache split, and
that is all a Claude Code session showed: the plugin sent neither.

The attribute is sizes, never content, so it is sent with capture off too.
It is the sink's v1 shape (argus-core apps/sink/internal/attribution/
sizes.go): the history up to the last assistant message folded into per-role
and per-tool byte sums, the messages after it in detail. What the plugin
cannot see -- Claude Code's system prompt and tool definitions -- is left
out, and the sink files the difference under `unattributed`.
"""
import json

from rius_cc import spans, state, transcript

FIXTURE = "multi_line_response.jsonl"


def _nbytes(text):
    return len(text.encode("utf-8"))


def _gens(fixtures_dir, capture=True, one_at_a_time=False):
    path = str(fixtures_dir / FIXTURE)
    entries, _ = transcript.read_from(path, 0)
    st = state.new_state()
    ctx = spans.Ctx(session_id="77777777-7777-7777-7777-777777777777",
                    cwd="/tmp/proj", git_branch="main", cc_version="2.1.284",
                    service_name="claude-code", capture_content=capture,
                    max_attr_bytes=32768)
    out = []
    batches = [[e] for e in entries] if one_at_a_time else [entries]
    for batch in batches:
        out += spans.build(batch, st, ctx, source_path=path)
    latest = {}
    for s in out:
        if s.kind_oi == "LLM":
            latest[s.span_id] = s
    by_out = {s.attributes["gen_ai.usage.output_tokens"]: s
              for s in latest.values()}
    return by_out, st


def _sizes(span):
    return json.loads(span.attributes["rius.context.sizes"])


def test_first_call_of_a_turn_is_the_prompt_in_detail(fixtures_dir):
    gens, _ = _gens(fixtures_dir)
    sizes = _sizes(gens[300])                       # msg_A
    assert sizes["version"] == 1
    assert sizes["input_messages"] == [
        {"role": "user",
         "parts": [{"type": "text", "bytes": _nbytes("check two files")}]}]
    assert "folded" not in sizes


def test_later_calls_fold_the_history_and_detail_the_tool_results(fixtures_dir):
    gens, _ = _gens(fixtures_dir)
    sizes = _sizes(gens[120])                       # msg_B
    folded = sizes["folded"]
    # The prompt, and msg_A: its text and its two Read calls' arguments.
    assert folded["user_bytes"] == _nbytes("check two files")
    assert folded["assistant_bytes"] == _nbytes("Reading both files now.")
    read_args = (_nbytes(json.dumps({"file_path": "/tmp/one"}))
                 + _nbytes(json.dumps({"file_path": "/tmp/two"})))
    assert folded["tools"] == [{"tool": "Read", "bytes": read_args}]
    assert folded["messages"] == 2
    # After msg_A: the two Read results, each tied to its tool by name.
    assert sizes["input_messages"] == [
        {"role": "user", "parts": [{"type": "tool_call_response",
                                    "tool": "Read",
                                    "bytes": _nbytes("body of file one")}]},
        {"role": "user", "parts": [{"type": "tool_call_response",
                                    "tool": "Read",
                                    "bytes": _nbytes("body of file two")}]}]


def test_a_streamed_response_does_not_see_its_own_tool_results(fixtures_dir):
    """msg_B's first Bash result arrived while msg_B was still streaming; it
    is context for msg_C, not for msg_B."""
    gens, _ = _gens(fixtures_dir)
    c = _sizes(gens[20])                            # msg_C
    tools = {t["tool"]: t["bytes"] for t in c["folded"]["tools"]}
    bash_args = (_nbytes(json.dumps({"command": "ls /tmp"}))
                 + _nbytes(json.dumps({"command": "wc -l /tmp/one"})))
    assert tools["Bash"] == bash_args + _nbytes("one two")
    assert c["input_messages"] == [
        {"role": "user", "parts": [{"type": "tool_call_response",
                                    "tool": "Bash",
                                    "bytes": _nbytes("3 /tmp/one")}]}]


def test_sizes_do_not_depend_on_hook_cadence(fixtures_dir):
    whole, _ = _gens(fixtures_dir)
    split, _ = _gens(fixtures_dir, one_at_a_time=True)
    for k in whole:
        assert _sizes(split[k]) == _sizes(whole[k])


def test_sizes_are_sent_with_capture_off_and_hold_no_content(fixtures_dir):
    gens, st = _gens(fixtures_dir, capture=False, one_at_a_time=True)
    for span in gens.values():
        assert "output.value" not in span.attributes
        raw = span.attributes["rius.context.sizes"]
        for secret in ("check two files", "Reading both files", "body of file",
                       "/tmp/one", "wc -l"):
            assert secret not in raw
            assert secret not in json.dumps(st)


def test_a_compaction_starts_the_history_again():
    rows = [
        {"type": "user", "uuid": "u1", "timestamp": "2026-09-30T10:00:00Z",
         "sessionId": "s", "promptId": "p1",
         "message": {"role": "user", "content": "x" * 5000}},
        {"type": "assistant", "uuid": "a1", "timestamp": "2026-09-30T10:00:01Z",
         "sessionId": "s", "message": {"id": "m1", "content": [
             {"type": "text", "text": "y" * 3000}],
             "usage": {"input_tokens": 1, "output_tokens": 1}}},
        {"type": "user", "uuid": "u2", "timestamp": "2026-09-30T10:05:00Z",
         "sessionId": "s", "promptId": "p2", "isCompactSummary": True,
         "message": {"role": "user", "content": "summary of it all"}},
        {"type": "assistant", "uuid": "a2", "timestamp": "2026-09-30T10:05:01Z",
         "sessionId": "s", "message": {"id": "m2", "content": [
             {"type": "text", "text": "ok"}],
             "usage": {"input_tokens": 1, "output_tokens": 2}}},
    ]
    entries = [transcript.parse_line(json.dumps(r)) for r in rows]
    ctx = spans.Ctx(session_id="s", cwd="/tmp", git_branch="", cc_version="",
                    service_name="claude-code", capture_content=False,
                    max_attr_bytes=32768)
    out = spans.build(entries, state.new_state(), ctx)
    after = [s for s in out if s.kind_oi == "LLM"
             and s.attributes["gen_ai.usage.output_tokens"] == 2][0]
    sizes = _sizes(after)
    assert "folded" not in sizes
    assert sizes["input_messages"] == [
        {"role": "user",
         "parts": [{"type": "text", "bytes": _nbytes("summary of it all")}]}]
