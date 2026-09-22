import pytest

from rius_cc import spans, transcript


def _ctx(session_id):
    return spans.Ctx(session_id=session_id, cwd="/tmp/proj", git_branch="main",
                     cc_version="2.1.278", service_name="claude-code",
                     capture_content=True, max_attr_bytes=32768)


def _new_state():
    return {"offset": 0, "open_tools": {}, "open_turns": {}, "root_started": False,
            "root_start_ns": 0, "last_ns": 0, "open_task_spans": []}


def test_ids_are_derived_and_stable():
    """Literal expectations, as spec section 10 requires. If the derivation
    ever changes, in-flight sessions orphan their spans and a resumed session
    silently starts a second trace. The previous version of this test asserted
    only shape and determinism, so swapping sha256 for md5 would have passed
    it -- it could not fail."""
    assert spans.trace_id_for("abc") == "ba7816bf8f01cfea414140de5dae2223"
    assert spans.span_id_for("abc") == "ba7816bf8f01cfea"

    sid = "11111111-1111-1111-1111-111111111111"
    assert spans.trace_id_for(sid) == "bafde89c041e1756082b933aaf16cad8"
    assert spans.span_id_for("session:" + sid) == "d4dd0dd4af7527a8"
    assert spans.span_id_for("turn:p1") == "a5817ee2869029be"
    assert spans.span_id_for("toolu_1") == "4838a0c252e24a61"

    # and the shape the OTLP encoder relies on
    assert len(spans.trace_id_for("abc")) == 32
    assert len(spans.span_id_for("abc")) == 16
    assert spans.trace_id_for("abc") != spans.trace_id_for("abd")


def test_root_span_has_empty_parent_and_is_pending_first(fixtures_dir):
    entries, _ = transcript.read_from(str(fixtures_dir / "simple.jsonl"), 0)
    st = _new_state()
    out = spans.build(entries, st, _ctx(entries[0].session_id))
    root = [s for s in out if s.kind_oi == "AGENT"][0]
    assert root.parent_span_id is None
    assert root.pending is True
    assert root.attributes["session.id"] == entries[0].session_id
    assert "input.value" not in root.attributes


def test_generation_span_carries_usage(fixtures_dir):
    entries, _ = transcript.read_from(str(fixtures_dir / "simple.jsonl"), 0)
    st = _new_state()
    out = spans.build(entries, st, _ctx(entries[0].session_id))
    gen = [s for s in out if s.kind_oi == "LLM"][0]
    a = gen.attributes
    assert a["gen_ai.request.model"] == "claude-opus-5"
    assert a["gen_ai.usage.input_tokens"] == 10
    assert a["gen_ai.usage.output_tokens"] == 5
    assert a["gen_ai.usage.cache_read.input_tokens"] == 100
    assert a["gen_ai.usage.cache_creation.input_tokens"] == 200
    assert a["gen_ai.operation.name"] == "chat"
    assert a["gen_ai.response.finish_reasons"] == ["end_turn"]
    assert gen.end_ns > gen.start_ns
    assert gen.pending is False


def test_tool_span_spans_request_to_result(fixtures_dir):
    entries, _ = transcript.read_from(str(fixtures_dir / "tool_call.jsonl"), 0)
    st = _new_state()
    out = spans.build(entries, st, _ctx(entries[0].session_id))
    final_tools = [s for s in out if s.kind_oi == "TOOL" and not s.pending]
    assert len(final_tools) == 1
    tool = final_tools[0]
    assert tool.attributes["gen_ai.tool.name"] == "Read"
    assert tool.end_ns - tool.start_ns == 2_000_000_000   # 10:00:01 -> 10:00:03
    assert tool.status_code != "ERROR"
    assert st["open_tools"] == {}                          # resolved and cleared


def test_tool_error_sets_error_status(fixtures_dir):
    entries, _ = transcript.read_from(str(fixtures_dir / "tool_error.jsonl"), 0)
    st = _new_state()
    out = spans.build(entries, st, _ctx(entries[0].session_id))
    tool = [s for s in out if s.kind_oi == "TOOL" and not s.pending][0]
    assert tool.status_code == "ERROR"


def test_tool_left_open_across_two_builds(fixtures_dir):
    """A tool_use in one batch, its result in the next -- the real hook cadence."""
    entries, _ = transcript.read_from(str(fixtures_dir / "tool_call.jsonl"), 0)
    st = _new_state()
    first = spans.build(entries[:2], st, _ctx(entries[0].session_id))
    assert "toolu_1" in st["open_tools"]
    assert [s for s in first if s.kind_oi == "TOOL"][0].pending is True
    second = spans.build(entries[2:], st, _ctx(entries[0].session_id))
    closed = [s for s in second if s.kind_oi == "TOOL" and not s.pending]
    assert len(closed) == 1
    assert st["open_tools"] == {}


# --- subagent sidechains (spec section 10 fixture; open_task_spans) -------

def test_sidechain_spans_hang_off_the_task_tool_span(fixtures_dir):
    """I8: the README drew an AGENT span for the subagent. The code emits no
    such span -- sidechain entries become ordinary LLM spans RE-PARENTED to
    the open Task tool span."""
    entries, _ = transcript.read_from(str(fixtures_dir / "subagent.jsonl"), 0)
    st = _new_state()
    out = spans.build(entries, st, _ctx(entries[0].session_id))

    task_span_id = spans.span_id_for("toolu_task")
    by_id = {s.span_id: s for s in out}

    sidechain_llm = by_id[spans.span_id_for("s1")]
    assert sidechain_llm.kind_oi == "LLM"
    assert sidechain_llm.parent_span_id == task_span_id

    # the subagent's own tool call hangs off the subagent's generation
    sub_tool = [s for s in out
                if s.span_id == spans.span_id_for("toolu_sub") and not s.pending][0]
    assert sub_tool.parent_span_id == sidechain_llm.span_id
    assert sub_tool.attributes["gen_ai.tool.name"] == "Read"

    # exactly one AGENT span: the session root. No subagent AGENT span exists.
    agents = [s for s in out if s.kind_oi == "AGENT"]
    assert len(agents) == 1
    assert agents[0].span_id == spans.span_id_for("session:" + entries[0].session_id)

    # the main-thread generation is NOT re-parented
    assert by_id[spans.span_id_for("a1")].parent_span_id == \
        spans.span_id_for("turn:p1")


def test_task_span_is_tracked_and_released(fixtures_dir):
    entries, _ = transcript.read_from(str(fixtures_dir / "subagent.jsonl"), 0)
    st = _new_state()
    ctx = _ctx(entries[0].session_id)

    # build in two batches, the real hook cadence: the Task span must survive
    # in persisted state across invocations.
    spans.build(entries[:2], st, ctx)
    assert st["open_task_spans"] == [spans.span_id_for("toolu_task")]

    spans.build(entries[2:5], st, ctx)
    assert st["open_task_spans"] == [spans.span_id_for("toolu_task")], \
        "a nested non-Task tool must not release the Task span"

    spans.build(entries[5:], st, ctx)
    assert st["open_task_spans"] == []
    assert st["open_tools"] == {}


def test_a_sidechain_user_entry_does_not_open_a_turn(fixtures_dir):
    entries, _ = transcript.read_from(str(fixtures_dir / "subagent.jsonl"), 0)
    st = _new_state()
    spans.build(entries, st, _ctx(entries[0].session_id))
    assert list(st["open_turns"]) == ["p1"]


def test_span_ids_identical_across_replay(fixtures_dir):
    entries, _ = transcript.read_from(str(fixtures_dir / "tool_call.jsonl"), 0)
    a = spans.build(entries, _new_state(), _ctx(entries[0].session_id))
    b = spans.build(entries, _new_state(), _ctx(entries[0].session_id))
    assert [s.span_id for s in a] == [s.span_id for s in b]
    assert [s.parent_span_id for s in a] == [s.parent_span_id for s in b]


def test_pending_spans_never_carry_content(fixtures_dir):
    entries, _ = transcript.read_from(str(fixtures_dir / "tool_call.jsonl"), 0)
    out = spans.build(entries[:2], _new_state(), _ctx(entries[0].session_id))
    for s in out:
        if s.pending:
            assert "input.value" not in s.attributes
            assert "output.value" not in s.attributes
            assert s.attributes["glassflow.span.pending"] is True


def test_capture_content_false_strips_content(fixtures_dir):
    """C3: uses the ERROR fixture deliberately. The non-error fixture could
    never have caught status_message, which bypassed the capture gate
    entirely and carried a failed command's stdout+stderr off the machine."""
    entries, _ = transcript.read_from(str(fixtures_dir / "tool_error.jsonl"), 0)
    ctx = _ctx(entries[0].session_id)
    ctx.capture_content = False
    out = spans.build(entries, _new_state(), ctx)
    for s in out:
        assert "input.value" not in s.attributes
        assert "output.value" not in s.attributes
        assert "ENOENT" not in (s.status_message or ""), \
            "tool error output leaked into Status.message with capture off"
    tool = [s for s in out if s.kind_oi == "TOOL" and not s.pending][0]
    assert tool.status_code == "ERROR"          # structure survives
    # Fixed string, no content -- and self-explanatory, so a viewer reading
    # the span knows the detail was withheld on purpose rather than lost.
    assert tool.status_message == \
        "tool error (detail withheld: RIUS_CAPTURE_CONTENT=false)"
    assert tool.status_message == spans.TOOL_ERROR_WITHHELD
    # structure survives
    assert any(s.attributes.get("gen_ai.tool.name") == "Read" for s in out)


def test_capture_content_true_keeps_the_tool_error_message(fixtures_dir):
    entries, _ = transcript.read_from(str(fixtures_dir / "tool_error.jsonl"), 0)
    out = spans.build(entries, _new_state(), _ctx(entries[0].session_id))
    tool = [s for s in out if s.kind_oi == "TOOL" and not s.pending][0]
    assert tool.status_message == "ENOENT"


def test_prompt_text_is_not_persisted_when_capture_is_off(fixtures_dir):
    """I6: open_turns is written verbatim to ~/.claude/rius/state/<sid>.json
    by state.save. The text was only SENT when capture was on, but it was
    always WRITTEN to disk in plaintext."""
    import json as _json
    entries, _ = transcript.read_from(str(fixtures_dir / "simple.jsonl"), 0)
    ctx = _ctx(entries[0].session_id)
    ctx.capture_content = False
    st = _new_state()
    spans.build(entries, st, ctx)
    assert st["open_turns"], "expected an open turn to have been recorded"
    assert "hello" not in _json.dumps(st), "prompt text was persisted to state"
    for turn in st["open_turns"].values():
        assert turn["text"] == ""


def test_prompt_text_is_persisted_when_capture_is_on(fixtures_dir):
    entries, _ = transcript.read_from(str(fixtures_dir / "simple.jsonl"), 0)
    st = _new_state()
    ctx = _ctx(entries[0].session_id)
    spans.build(entries, st, ctx)
    assert [t["text"] for t in st["open_turns"].values()] == ["hello"]
    out = spans.finalize_turn(st, ctx, now_ns=st["last_ns"] + 1)
    assert out[0].attributes["input.value"] == "hello"


def test_truncate_marks_what_it_removed():
    out = spans.truncate("x" * 100, 10)
    assert out.startswith("x" * 10)
    assert "[truncated 90 bytes]" in out
    assert spans.truncate("short", 100) == "short"


def test_truncate_measures_and_slices_in_the_same_unit():
    """It measured BYTES and sliced CHARACTERS, so a non-ASCII value came
    out up to 4x over the cap and the reported byte count was wrong."""
    value = "é" * 100                       # 200 bytes, 100 characters
    out = spans.truncate(value, 10)
    head = out.split(" …[truncated")[0]
    assert len(head.encode("utf-8")) <= 10, "truncated value is still over the cap"
    assert "[truncated 190 bytes]" in out

    # a cut landing mid-codepoint must drop the partial character, not
    # produce a replacement char and not raise
    head11 = spans.truncate(value, 11).split(" …[truncated")[0]
    assert head11 == "é" * 5
    assert "�" not in head11

    # a 4-byte codepoint, the worst case for the old slice
    emoji = spans.truncate("🙂" * 50, 6).split(" …[truncated")[0]
    assert len(emoji.encode("utf-8")) <= 6


def test_finalize_session_emits_no_root_if_one_never_started():
    """I5: open a session in an enabled folder, type nothing, quit. build()
    never ran, so root_start_ns is 0 and the closing root span started at the
    Unix epoch -- a 56-year span in the waterfall."""
    st = _new_state()
    ctx = _ctx("s-empty")
    out = spans.finalize_session(st, ctx, now_ns=1_790_071_200_000_000_000)
    assert [s for s in out if s.kind_oi == "AGENT"] == []


def test_finalize_session_never_emits_an_epoch_start():
    """Belt and braces: a state that says a root started but has no start
    timestamp must still not date the span to 1970."""
    st = _new_state()
    st["root_started"] = True          # started, start_ns somehow lost
    ctx = _ctx("s-odd")
    now = 1_790_071_200_000_000_000
    root = [s for s in spans.finalize_session(st, ctx, now_ns=now)
            if s.kind_oi == "AGENT"][0]
    assert root.start_ns == now


def test_finalize_session_closes_root(fixtures_dir):
    entries, _ = transcript.read_from(str(fixtures_dir / "simple.jsonl"), 0)
    st = _new_state()
    ctx = _ctx(entries[0].session_id)
    spans.build(entries, st, ctx)
    out = spans.finalize_session(st, ctx, now_ns=st["last_ns"] + 1_000_000_000)
    root = [s for s in out if s.kind_oi == "AGENT"][0]
    assert root.pending is False
    assert root.parent_span_id is None
    assert root.end_ns > root.start_ns
    assert root.span_id == spans.span_id_for("session:" + ctx.session_id)


# --- N3: harness-injected turns are not user prompts ---------------------

def test_system_injected_turns_are_marked_not_dropped(fixtures_dir):
    """N3: of 10 turn spans in the live session, several had an input.value
    of <local-command-caveat>, <task-notification>, <agent-message ...> or
    <bash-input>. Those are harness-injected, not things the user typed, and
    a turn list that cannot tell them apart is misleading. They are still
    real work, so they are marked rather than dropped."""
    entries, _ = transcript.read_from(str(fixtures_dir / "system_turns.jsonl"), 0)
    st = _new_state()
    ctx = _ctx(entries[0].session_id)
    out = spans.build(entries, st, ctx)

    turns = [s for s in out if s.kind_oi == "CHAIN"]
    assert len(turns) == 6, "a system turn must be marked, never dropped"

    by_prompt = dict((p, t["source"]) for p, t in st["open_turns"].items())
    assert by_prompt == {"p1": "user", "p2": "system", "p3": "system",
                         "p4": "system", "p5": "system", "p6": "user"}

    # the marker is on the pending span too -- it is not content, and the UI
    # needs it while the turn is still running
    sources = dict((s.attributes["session.id"] and
                    s.start_ns, s.attributes.get("cc.turn.source"))
                   for s in turns)
    assert set(sources.values()) == {"user", "system"}

    final = spans.finalize_turn(st, ctx, now_ns=st["last_ns"] + 1)
    marks = sorted(s.attributes["cc.turn.source"] for s in final)
    assert marks == ["system", "system", "system", "system", "user", "user"]


def test_turn_source_detection_is_anchored_to_the_start_of_the_text():
    """A prompt that merely MENTIONS one of the wrappers is still a user
    prompt. Substring matching would have mislabelled it."""
    assert spans.turn_source_for("<bash-input>ls</bash-input>") == "system"
    assert spans.turn_source_for("  <task-notification>x") == "system"
    assert spans.turn_source_for('<agent-message agent="x">hi') == "system"
    assert spans.turn_source_for("why does <bash-input> show up?") == "user"
    assert spans.turn_source_for("hello") == "user"
    assert spans.turn_source_for("") == "user"


def test_tool_input_is_still_captured_when_capture_is_on(fixtures_dir):
    """The counterpart to the state-hygiene fix: open_tools no longer keeps
    a tool's input when capture is off, so prove it still keeps it -- and
    still fills input.value from it -- when capture is on."""
    entries, _ = transcript.read_from(str(fixtures_dir / "tool_call.jsonl"), 0)
    st = _new_state()
    ctx = _ctx(entries[0].session_id)
    spans.build(entries[:2], st, ctx)
    assert st["open_tools"]["toolu_1"]["input_json"] == '{"file_path": "/tmp/x"}'
    out = spans.build(entries[2:], st, ctx)
    tool = [s for s in out if s.kind_oi == "TOOL" and not s.pending][0]
    assert tool.attributes["input.value"] == '{"file_path": "/tmp/x"}'
    assert tool.attributes["output.value"] == "file body"
