"""`--agent codex` end to end through the exporter: hook events re-read the
rollout, subagents land under the spawn_agent call that started them, and
the sweep closes a dead Codex session's trace after an hour.

The subagent fixtures are one real codex-cli 0.144.1 run against a mock
model: a `tools please` turn, then a resumed turn that spawns a `default`
subagent (which runs `echo from-sub`) and waits for it. Long instructions are
trimmed and paths rewritten.
"""
import pathlib
import shutil

import pytest

import exporter
from rius_cc import agent, codex_spans, config, spans, state

from tests import signed_in

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "codex"
PARENT = "01a10b65-83da-7ab0-99f7-8e8364ecc4b0"
CHILD = "01a10b65-895d-7222-9bfe-eeea5accdad2"
ENV = {}
HOUR_NS = 3600 * 10**9


@pytest.fixture
def codex_home(tmp_path):
    home = tmp_path / ".codex"
    with agent.using(agent.CODEX):
        signed_in.sign_in(str(tmp_path))
        config.write_path_rules(str(tmp_path), {"enabled_paths": ["/tmp"]})
        yield home


@pytest.fixture
def sent(monkeypatch):
    """(resource attributes, spans) per accepted batch."""
    batches, pending = [], {}

    def encode(resource, out):
        pending["batch"] = (resource, list(out))
        return b"x"

    def export(endpoint, key, body, timeout=5.0):
        batches.append(pending.pop("batch"))
        return 200

    monkeypatch.setattr(exporter.otlp, "encode", encode)
    monkeypatch.setattr(exporter.otlp, "export", export)
    return batches


@pytest.fixture
def rollouts(tmp_path):
    """The parent's and the child's rollout, side by side as Codex writes
    them."""
    folder = tmp_path / "sessions"
    shutil.copytree(str(FIXTURES / "subagent"), str(folder))
    return {thread: next(folder.glob("rollout-*-%s.jsonl" % thread))
            for thread in (PARENT, CHILD)}


def _run(event, home, rollout, **extra):
    payload = {"session_id": PARENT, "cwd": "/tmp/proj",
               "transcript_path": str(rollout), "hook_event_name": event}
    payload.update(extra)
    return exporter.run(event, payload, ENV, str(home))


def _spans(batches):
    return [s for _, out in batches for s in out]


def _finished(batches):
    return {s.span_id: s for s in _spans(batches) if not s.pending}


def _state(home, session_id=PARENT):
    return state.load(session_id, str(home))


def test_a_stop_ships_the_rollout_as_codex_spans(codex_home, tmp_path, sent,
                                                 rollouts):
    _run("Stop", tmp_path, rollouts[PARENT])
    resource = sent[0][0]
    assert resource["service.name"] == "codex"
    assert resource["codex.version"] == "0.144.1"
    assert not any(k.startswith("cc.") for k in resource)
    names = [s.name for s in _spans(sent)]
    assert names[0] == "codex session" and "mock-model" in names
    trace_ids = {s.trace_id for s in _spans(sent)}
    assert trace_ids == {spans.trace_id_for(PARENT)}
    st = _state(tmp_path)
    assert st["offset"] == rollouts[PARENT].stat().st_size
    assert (codex_home / "rius" / "state" / (PARENT + ".json")).exists()


def test_the_subagent_hangs_under_its_spawn_call(codex_home, tmp_path, sent,
                                                 rollouts):
    _run("Stop", tmp_path, rollouts[PARENT])
    finished = _finished(sent)
    spawn = next(s for s in finished.values()
                 if s.name == codex_spans.SPAWN_TOOL)
    sub_root = next(s for s in _spans(sent) if s.kind_oi == "AGENT"
                    and s.parent_span_id == spawn.span_id)
    assert sub_root.name == "default"
    assert sub_root.attributes["gen_ai.agent.name"] == "default"
    assert sub_root.attributes["codex.subagent.id"] == CHILD
    sub_turns = [s for s in finished.values()
                 if s.parent_span_id == sub_root.span_id]
    assert [s.name for s in sub_turns] == ["turn"]
    sub_tools = [s for s in finished.values() if s.kind_oi == "TOOL"
                 and finished.get(s.parent_span_id) is not None
                 and finished[s.parent_span_id].parent_span_id
                 == sub_turns[0].span_id]
    assert [s.name for s in sub_tools] == ["exec_command"]


def test_a_subagent_hook_reads_its_own_rollout_not_the_parents_offset(
        codex_home, tmp_path, sent, rollouts):
    """Codex fires a subagent's hooks under the PARENT's session id with the
    child's rollout as transcript_path. Read with the parent's offset, the
    child's lines would land in the parent's turns."""
    _run("UserPromptSubmit", tmp_path, rollouts[PARENT])
    parent_offset = _state(tmp_path)["offset"]
    _run("PostToolUse", tmp_path, rollouts[CHILD], agent_id=CHILD,
         agent_type="default", tool_name="Bash")
    st = _state(tmp_path)
    assert st["offset"] == parent_offset
    assert st["transcript_path"] == str(rollouts[PARENT])
    assert st["codex_subs"][CHILD]["path"] == str(rollouts[CHILD])
    assert st["codex_subs"][CHILD]["offset"] == rollouts[CHILD].stat().st_size
    parent_turns = [s for s in _spans(sent) if s.name == "turn"
                    and s.parent_span_id == spans.span_id_for(
                        "session:" + PARENT)]
    assert len({s.span_id for s in parent_turns}) == 2


def test_subagent_stop_names_the_childs_rollout(codex_home, tmp_path, sent,
                                                rollouts, monkeypatch):
    monkeypatch.setattr(exporter.codex_session, "_beside_parent",
                        lambda st, agent_id: "")
    _run("SubagentStop", tmp_path, rollouts[PARENT], agent_id=CHILD,
         agent_type="default",
         agent_transcript_path=str(rollouts[CHILD]))
    assert any(s.attributes.get("codex.subagent.id") == CHILD
               for s in _spans(sent))


def test_a_child_with_no_hook_yet_is_found_beside_its_parent(
        codex_home, tmp_path, sent, rollouts):
    _run("PostToolUse", tmp_path, rollouts[PARENT])
    assert _state(tmp_path)["codex_subs"][CHILD]["path"] == str(rollouts[CHILD])


def test_reading_in_steps_sends_what_one_read_sends(codex_home, tmp_path,
                                                    sent, rollouts):
    _run("Stop", tmp_path, rollouts[PARENT])
    whole = {s.span_id for s in _finished(sent).values()}

    other = tmp_path / "other"
    other.mkdir()
    lines = rollouts[PARENT].read_bytes().splitlines(keepends=True)
    growing = other / rollouts[PARENT].name
    shutil.copy(str(rollouts[CHILD]), str(other / rollouts[CHILD].name))
    del sent[:]
    with agent.using(agent.CODEX):
        signed_in.sign_in(str(other))
        config.write_path_rules(str(other), {"enabled_paths": ["/tmp"]})
        for cut in (5, 20, len(lines)):
            growing.write_bytes(b"".join(lines[:cut]))
            exporter.run("PostToolUse", {
                "session_id": PARENT, "cwd": "/tmp/proj",
                "transcript_path": str(growing)}, ENV, str(other))
    assert {s.span_id for s in _finished(sent).values()} == whole


def test_a_disabled_folder_sends_nothing(codex_home, tmp_path, sent, rollouts):
    config.write_path_rules(str(tmp_path), {"disabled_paths": ["/tmp"]})
    _run("Stop", tmp_path, rollouts[PARENT])
    assert sent == []


# --- the sweep: Codex has no SessionEnd ---------------------------------------

def _open_dead_session(tmp_path, rollouts):
    _run("Stop", tmp_path, rollouts[PARENT])
    st = _state(tmp_path)
    assert state.trace_is_open(st)
    return st["last_ns"]


def _sweep(tmp_path, now_ns):
    cfg = config.resolve("other", "/tmp/new", ENV, str(tmp_path))
    exporter.sweep_stale(cfg, "other", str(tmp_path), now_ns=now_ns)


def test_a_dead_codex_session_closes_after_an_hour(codex_home, tmp_path,
                                                   sent, rollouts):
    last_ns = _open_dead_session(tmp_path, rollouts)
    del sent[:]
    _sweep(tmp_path, last_ns + HOUR_NS // 2)
    assert sent == []
    _sweep(tmp_path, last_ns + HOUR_NS + 1)
    closing = _spans(sent)
    roots = {s.name: s for s in closing if s.kind_oi == "AGENT"}
    assert set(roots) == {"codex session", "default"}
    assert all(not s.pending and s.end_ns == last_ns for s in roots.values())
    assert not state.trace_is_open(_state(tmp_path))


def test_a_live_codex_process_keeps_its_trace_open(codex_home, tmp_path, sent,
                                                   rollouts, monkeypatch):
    last_ns = _open_dead_session(tmp_path, rollouts)
    del sent[:]
    monkeypatch.setattr(exporter.platform_compat, "pid_alive",
                        lambda pid: True)
    st = _state(tmp_path)
    st["cc_pid"] = 4242
    state.save(PARENT, str(tmp_path), st)
    _sweep(tmp_path, last_ns + 5 * HOUR_NS)
    assert sent == []


def test_claude_code_keeps_its_twelve_hours():
    assert exporter.STALE_AFTER_S == 12 * 3600
    assert exporter._stale_after_s() == exporter.STALE_AFTER_S
    with agent.using(agent.CODEX):
        assert exporter._stale_after_s() == 3600


def test_the_closing_spans_carry_no_content(codex_home, tmp_path, sent,
                                            rollouts):
    lines = rollouts[PARENT].read_bytes().splitlines(keepends=True)
    rollouts[PARENT].write_bytes(b"".join(lines[:12]))
    last_ns = _open_dead_session(tmp_path, rollouts)
    del sent[:]
    _sweep(tmp_path, last_ns + 2 * HOUR_NS)
    assert sent
    for s in _spans(sent):
        assert "input.value" not in s.attributes
        assert "output.value" not in s.attributes


def test_a_subagent_id_that_is_not_an_id_is_ignored(codex_home, tmp_path,
                                                    sent, rollouts):
    _run("SubagentStop", tmp_path, rollouts[PARENT], agent_id="../../x",
         agent_transcript_path=str(rollouts[CHILD]))
    assert "../../x" not in _state(tmp_path).get("codex_subs", {})


def test_call_ids_with_underscores_still_make_tool_spans(codex_home, tmp_path,
                                                        sent):
    _run("Stop", tmp_path, FIXTURES / "mock_tools_mcp_resume.jsonl")
    ids = {codex_spans.span_id_for(call)
           for call in ("call_t3_5", "call_t3_6")}
    assert ids <= set(_finished(sent))
