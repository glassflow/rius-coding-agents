"""Codex subagents beyond the simple case, from real codex-cli 0.144.1 runs
against a mock model:

- fork/: `forkspawn please` spawns a subagent with fork_context, so the
  child's rollout opens with a copy of the parent's history (its turns,
  token counts and the parent's still-open turn) under the parent's ids.
- nested/: with agents.max_depth = 2 the child spawns a grandchild. The
  grandchild's SubagentStop names the CHILD's rollout as transcript_path.

What the backend keeps is one row per (start time, trace, span id); a span
id sent with two start times is two rows, and its tokens count twice.
"""
import json
import pathlib
import shutil

from rius_cc import codex_rollout, spans, state

from tests.test_codex_export import ENV, codex_home, exporter, sent  # noqa: F401

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "codex"
FORK_PARENT = "01a10c9e-8b17-7250-9602-a0f21c5368b4"
FORK_CHILD = "01a10c9e-9508-71f3-b354-3039bfd6532d"
NESTED_ROOT = "01a10ca3-ad67-7ec1-a93b-2248e7bc1e90"


def _rollouts(tmp_path, name):
    folder = tmp_path / "sessions"
    shutil.copytree(str(FIXTURES / name), str(folder))
    return {p.stem[-36:]: p for p in folder.glob("rollout-*.jsonl")}


def _run(home, session_id, event, transcript, **extra):
    payload = {"session_id": session_id, "cwd": "/tmp/proj",
               "transcript_path": str(transcript), "hook_event_name": event}
    payload.update(extra)
    return exporter.run(event, payload, ENV, str(home))


def _rows(batches):
    """The backend's view: the last row per (start, span id)."""
    rows = {}
    for _, out in batches:
        for s in out:
            key = (s.start_ns, s.span_id)
            if not s.pending or key not in rows:
                rows[key] = s
    return rows


def _llm_input_tokens(rows):
    return sum(s.attributes.get("gen_ai.usage.input_tokens") or 0
               for s in rows.values() if s.kind_oi == "LLM")


def _one_row_per_span_id(rows):
    ids = [span_id for _, span_id in rows]
    return sorted(i for i in set(ids) if ids.count(i) > 1)


def _own_turns(path):
    records, _ = codex_rollout.read_from(str(path), 0)
    return [r.get("turn_id") for r in records if r.kind == codex_rollout.TURN_START]


def test_a_forked_subagent_does_not_replay_its_parents_history(
        codex_home, tmp_path, sent):
    rollouts = _rollouts(tmp_path, "fork")
    _run(tmp_path, FORK_PARENT, "Stop", rollouts[FORK_PARENT])
    rows = _rows(sent)
    assert _one_row_per_span_id(rows) == []
    # The parent's own calls (30000) and the child's (7000 + 8000), never
    # the child's copy of the parent's.
    assert _llm_input_tokens(rows) == 45000
    child_root = spans.span_id_for("subagent:" + FORK_CHILD)
    child_turns = [s for s in rows.values() if s.name == "turn"
                   and s.parent_span_id == child_root]
    assert [s.attributes.get("codex.turn.id") for s in child_turns] == [
        "01a10c9e-952f-7943-a7ff-eff3e50d2101"]


def test_a_nested_subagent_stop_keeps_the_sessions_rollout(
        codex_home, tmp_path, sent):
    rollouts = _rollouts(tmp_path, "nested")
    hooks = json.loads((FIXTURES / "nested" / "hooks.json").read_text())["hooks"]
    for hook in hooks:
        extra = {k: hook[k] for k in ("agent_id", "agent_type") if hook[k]}
        if hook["agent_transcript"]:
            extra["agent_transcript_path"] = str(rollouts[hook["agent_transcript"]])
        _run(tmp_path, NESTED_ROOT, hook["event"],
             rollouts[hook["transcript"]], **extra)
        st = state.load(NESTED_ROOT, str(tmp_path))
        assert st["transcript_path"] == str(rollouts[NESTED_ROOT]), hook
    rows = _rows(sent)
    assert _one_row_per_span_id(rows) == []
    assert _llm_input_tokens(rows) == 36000
    root = spans.span_id_for("session:" + NESTED_ROOT)
    root_turns = {s.attributes.get("codex.turn.id") for s in rows.values()
                  if s.name == "turn" and s.parent_span_id == root}
    assert root_turns == set(_own_turns(rollouts[NESTED_ROOT]))
