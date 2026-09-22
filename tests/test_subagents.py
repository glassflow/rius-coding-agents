"""Subagent drilldown: the work Claude Code writes to a SEPARATE transcript.

The main transcript contains zero sidechain entries. A subagent's entire run
lives in <session-dir>/subagents/agent-<id>.jsonl, linked to the spawning
`Agent` tool_use by `toolUseId` in the sibling .meta.json. In one real session
58% of all tokens and 71% of all generations were inside subagents, so a trace
that stops at the tool span reports less than half the true cost.
"""
import json
import shutil

from rius_cc import spans, state, subagents, transcript

SID = "55555555-5555-5555-5555-555555555555"
ORPHAN_SID = "66666666-6666-6666-6666-666666666666"


def _ctx(session_id, capture=True):
    return spans.Ctx(session_id=session_id, cwd="/tmp/proj", git_branch="main",
                     cc_version="2.1.278", service_name="claude-code",
                     capture_content=capture, max_attr_bytes=32768)


def _main_path(fixtures_dir):
    return str(fixtures_dir / "subagent_files" / (SID + ".jsonl"))


def _run(path, session_id, st=None, ctx=None):
    """Build the main transcript, then expand its subagents, as exporter does."""
    st = state.new_state() if st is None else st
    ctx = ctx or _ctx(session_id)
    entries, offset = transcript.read_from(path, st["offset"])
    st["offset"] = offset
    out = spans.build(entries, st, ctx)
    out += subagents.expand(st, ctx, subagents.dir_for(path, session_id))
    return out, st, ctx


def _by_id(out):
    return dict((s.span_id, s) for s in out)


# --- N1: the tool is called "Agent" in this Claude Code version ----------

def test_agent_tool_name_is_recognised_as_a_subagent_spawn(fixtures_dir):
    """spans.py only ever matched "Task". The live session emitted "Agent",
    so open_task_spans stayed empty and nothing was ever a subagent parent."""
    assert "Agent" in spans.SUBAGENT_TOOL_NAMES
    assert "Task" in spans.SUBAGENT_TOOL_NAMES

    entries, _ = transcript.read_from(_main_path(fixtures_dir), 0)
    st = state.new_state()
    spans.build(entries[:2], st, _ctx(SID))
    assert list(st["sub_links"]) == ["toolu_agent1"]
    assert st["sub_links"]["toolu_agent1"]["span_id"] == spans.span_id_for("toolu_agent1")


# --- N2: nesting, identity, attributes ----------------------------------

def test_subagent_spans_nest_under_the_spawning_tool_span(fixtures_dir):
    out, st, _ = _run(_main_path(fixtures_dir), SID)
    by_id = _by_id(out)

    tool_span_id = spans.span_id_for("toolu_agent1")
    agent_span_id = spans.span_id_for("subagent:agent-aaa111")

    agent = [s for s in out if s.span_id == agent_span_id and not s.pending][0]
    assert agent.kind_oi == "AGENT"
    assert agent.parent_span_id == tool_span_id

    # the subagent's own generations hang off ITS agent span, not the root
    sub_llm = [s for s in out
               if s.span_id == spans.span_id_for("sa1") and not s.pending][0]
    assert sub_llm.kind_oi == "LLM"
    assert sub_llm.parent_span_id == agent_span_id

    # and the subagent's tool call hangs off that generation
    sub_tool = [s for s in out
                if s.span_id == spans.span_id_for("toolu_sub_read") and not s.pending][0]
    assert sub_tool.parent_span_id == sub_llm.span_id
    assert sub_tool.attributes["gen_ai.tool.name"] == "Read"
    assert by_id  # sanity


def test_subagent_spans_carry_the_parent_session_trace_id(fixtures_dir):
    """One trace per session still holds: a subagent is a subtree, not a
    second trace."""
    out, _, _ = _run(_main_path(fixtures_dir), SID)
    assert out
    assert set(s.trace_id for s in out) == set([spans.trace_id_for(SID)])


def test_subagent_agent_span_is_stamped_from_meta_json(fixtures_dir):
    """gen_ai.agent.name is the key argus-core's sink filters on
    (docs/spans-query.md), so this is what makes a subagent addressable as a
    named agent in the UI rather than an anonymous span."""
    out, _, _ = _run(_main_path(fixtures_dir), SID)
    agent = [s for s in out
             if s.span_id == spans.span_id_for("subagent:agent-aaa111")
             and not s.pending][0]
    assert agent.attributes["gen_ai.agent.name"] == "general-purpose"
    assert agent.attributes["gen_ai.request.model"] == "haiku"
    assert agent.attributes["gen_ai.agent.description"] == "Explore the fixture tree"
    assert agent.attributes["cc.subagent.id"] == "agent-aaa111"
    assert agent.attributes["cc.subagent.depth"] == 1
    assert agent.attributes["session.id"] == SID
    assert agent.name == "general-purpose"
    assert agent.attributes["input.value"] == "explore the fixture tree"


def test_subagent_agent_span_starts_pending_and_then_closes(fixtures_dir):
    out, _, _ = _run(_main_path(fixtures_dir), SID)
    agent_span_id = spans.span_id_for("subagent:agent-aaa111")
    mine = [s for s in out if s.span_id == agent_span_id]
    assert [s.pending for s in mine] == [True, False]
    pending, final = mine
    # a pending span must never carry content, and the subagent's prompt and
    # description are content.
    assert "input.value" not in pending.attributes
    assert "gen_ai.agent.description" not in pending.attributes
    # ...but it must still be filterable by agent name while it is running
    assert pending.attributes["gen_ai.agent.name"] == "general-purpose"
    # the tool_result in the MAIN transcript is what ends the subagent
    assert final.end_ns == transcript._timestamp_ns("2026-09-22T10:00:09.000Z")
    assert final.start_ns == transcript._timestamp_ns("2026-09-22T10:00:01.000Z")
    assert final.status_code == "OK"


def test_subagent_token_usage_reaches_the_trace(fixtures_dir):
    """The number that forced this feature: without it, every one of these
    tokens is invisible and the session's reported cost is less than half."""
    out, _, _ = _run(_main_path(fixtures_dir), SID)
    sub_ids = set(["sa1", "sa3", "sa5", "sb1"])
    total_in = total_out = 0
    for s in out:
        if s.kind_oi != "LLM" or s.pending:
            continue
        total_in += s.attributes.get("gen_ai.usage.input_tokens", 0)
        total_out += s.attributes.get("gen_ai.usage.output_tokens", 0)
    # main a1 (10/5) + subagent 100/20 + 200/30 + 300/40 + nested 50/10
    assert total_in == 10 + 100 + 200 + 300 + 50
    assert total_out == 5 + 20 + 30 + 40 + 10
    emitted = set(s.span_id for s in out)
    for uuid in sub_ids:
        assert spans.span_id_for(uuid) in emitted


def test_subagent_turn_ids_do_not_collide_with_the_main_turn(fixtures_dir):
    """The subagent transcript carries the PARENT's promptId. A turn span
    keyed on promptId alone would collide with the main session's turn span
    and silently re-parent it under the subagent."""
    out, st, _ = _run(_main_path(fixtures_dir), SID)
    main_turn = spans.span_id_for("turn:p1")
    turns = [s for s in out if s.span_id == main_turn]
    assert len(turns) == 1
    assert turns[0].parent_span_id == spans.span_id_for("session:" + SID)
    assert list(st["open_turns"]) == ["p1"]


# --- recursion ----------------------------------------------------------

def test_turn_ids_in_a_subagent_scope_are_namespaced(fixtures_dir):
    """A subagent scope does not open turn spans today -- its generations
    hang off its AGENT span. If one ever does, the span id MUST carry the
    agent prefix: the subagent transcript repeats the parent's promptId, so
    an unprefixed key silently re-parents the main session's turn."""
    path = str(fixtures_dir / "subagent_files" / SID / "subagents"
               / "agent-aaa111.jsonl")
    entries, _ = transcript.read_from(path, 0)
    scope = spans.new_scope()
    out = spans.emit_entries(entries, scope, _ctx(SID), spans.trace_id_for(SID),
                             "rootrootrootroot", {}, depth=1,
                             key_prefix="agent:agent-aaa111:",
                             make_turns=True, inline_sidechains=False)
    turn = [s for s in out if s.kind_oi == "CHAIN"][0]
    assert turn.span_id == spans.span_id_for("agent:agent-aaa111:turn:p1")
    assert turn.span_id != spans.span_id_for("turn:p1")


def test_a_subagent_that_spawns_a_subagent_nests_two_deep(fixtures_dir):
    out, st, _ = _run(_main_path(fixtures_dir), SID)

    outer = spans.span_id_for("subagent:agent-aaa111")
    nested_tool = spans.span_id_for("toolu_agent2")
    nested = spans.span_id_for("subagent:agent-bbb222")

    nested_span = [s for s in out if s.span_id == nested and not s.pending][0]
    assert nested_span.parent_span_id == nested_tool
    assert nested_span.attributes["gen_ai.agent.name"] == "Explore"
    assert nested_span.attributes["cc.subagent.depth"] == 2

    # the tool that spawned it belongs to the OUTER subagent's generation
    tool = [s for s in out if s.span_id == nested_tool and not s.pending][0]
    assert tool.parent_span_id == spans.span_id_for("sa3")

    # the nested subagent's generation sits under the nested agent span
    gen = [s for s in out if s.span_id == spans.span_id_for("sb1")][0]
    assert gen.parent_span_id == nested

    assert st["sub_links"]["toolu_agent2"]["depth"] == 2
    assert outer in set(s.span_id for s in out)


def test_recursion_stops_at_the_depth_cap(fixtures_dir, monkeypatch):
    monkeypatch.setattr(subagents, "MAX_DEPTH", 1)
    out, _, _ = _run(_main_path(fixtures_dir), SID)
    ids = set(s.span_id for s in out)
    assert spans.span_id_for("subagent:agent-aaa111") in ids
    assert spans.span_id_for("subagent:agent-bbb222") not in ids


# --- incremental streaming ----------------------------------------------

def test_subagent_streams_incrementally_by_per_agent_offset(fixtures_dir, tmp_path):
    """Per-agent byte offsets in state, exactly like the main transcript:
    each hook event emits only what is new."""
    src = fixtures_dir / "subagent_files"
    dst = tmp_path / "subagent_files"
    shutil.copytree(str(src), str(dst))
    main = str(dst / (SID + ".jsonl"))
    sub = dst / SID / "subagents" / "agent-aaa111.jsonl"
    full = sub.read_text().splitlines(True)

    # the subagent has written only its first two entries so far
    sub.write_text("".join(full[:2]))
    # ...and the main transcript has not yet seen the closing tool_result
    main_lines = (dst / (SID + ".jsonl")).read_text().splitlines(True)
    (dst / (SID + ".jsonl")).write_text("".join(main_lines[:2]))

    first, st, ctx = _run(main, SID)
    assert st["sub_offsets"]["agent-aaa111"] == len("".join(full[:2]))
    assert spans.span_id_for("sa1") in set(s.span_id for s in first)
    assert spans.span_id_for("sa5") not in set(s.span_id for s in first)
    json.dumps(st)  # state must stay JSON-serializable

    # the rest arrives
    sub.write_text("".join(full))
    (dst / (SID + ".jsonl")).write_text("".join(main_lines))
    second, st, _ = _run(main, SID, st=st, ctx=ctx)

    ids = [s.span_id for s in second]
    assert spans.span_id_for("sa5") in ids
    assert ids.count(spans.span_id_for("sa1")) == 0, \
        "an already-exported subagent generation was re-emitted"
    assert st["sub_offsets"]["agent-aaa111"] == len("".join(full))


def test_nothing_new_means_no_subagent_spans(fixtures_dir):
    out, st, ctx = _run(_main_path(fixtures_dir), SID)
    assert out
    again = subagents.expand(st, ctx,
                             subagents.dir_for(_main_path(fixtures_dir), SID))
    assert again == []


# --- the fallback: no meta.json -----------------------------------------

def test_no_matching_meta_json_leaves_the_tool_span_alone(fixtures_dir):
    """Exact mapping or nothing. Never mtime, never ordering: guessing which
    subagent a tool call spawned puts one agent's tokens under another's name."""
    path = str(fixtures_dir / "subagent_orphan" / (ORPHAN_SID + ".jsonl"))
    out, st, _ = _run(path, ORPHAN_SID)
    assert [s for s in out if s.kind_oi == "AGENT" and not s.pending] == []
    tool = [s for s in out
            if s.span_id == spans.span_id_for("toolu_unknown") and not s.pending][0]
    assert tool.attributes["gen_ai.tool.name"] == "Agent"
    assert st["sub_offsets"] == {}


def test_a_missing_subagents_directory_is_not_an_error(fixtures_dir, tmp_path):
    st = state.new_state()
    ctx = _ctx(SID)
    entries, _ = transcript.read_from(_main_path(fixtures_dir), 0)
    spans.build(entries, st, ctx)
    assert subagents.expand(st, ctx, str(tmp_path / "nope" / "subagents")) == []


def test_dir_for_derives_the_session_directory():
    assert subagents.dir_for("/p/projects/-x/abc.jsonl", "abc") == \
        "/p/projects/-x/abc/subagents"
    assert subagents.dir_for("", "abc") == ""


# --- content capture ----------------------------------------------------

def test_capture_off_withholds_every_subagent_content_attribute(fixtures_dir):
    """Subagent prompts, descriptions and tool output are content too."""
    ctx = _ctx(SID, capture=False)
    out, st, _ = _run(_main_path(fixtures_dir), SID, ctx=ctx)
    assert out
    blob = json.dumps([s.attributes for s in out])
    for s in out:
        assert "input.value" not in s.attributes
        assert "output.value" not in s.attributes
        assert "gen_ai.agent.description" not in s.attributes
    assert "explore the fixture tree" not in blob
    assert "def resolve" not in blob
    # nor may it be persisted to the plaintext state file
    assert "explore the fixture tree" not in json.dumps(st)
