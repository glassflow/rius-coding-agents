import json

import pytest

import exporter
from rius_cc import config, state


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    (h / ".claude" / "rius").mkdir(parents=True)
    with open(config.path_rules_path(str(h)), "w") as fh:
        json.dump({"enabled_paths": ["/tmp"]}, fh)
    return str(h)


@pytest.fixture
def captured(monkeypatch):
    sent = []
    monkeypatch.setattr(exporter.otlp, "export",
                        lambda ep, key, body, timeout=5.0: sent.append((ep, key, body)) or 200)
    return sent


def _payload(fixtures_dir, name, session_id, event, cwd="/tmp/proj"):
    return {"session_id": session_id, "transcript_path": str(fixtures_dir / name),
            "cwd": cwd, "hook_event_name": event}


ENV = {"RIUS_API_KEY": "glassflow_k", "RIUS_ENDPOINT": "https://ingest.test"}


def test_exports_spans_and_advances_offset(home, captured, fixtures_dir):
    sid = "11111111-1111-1111-1111-111111111111"
    n = exporter.run("PostToolUse", _payload(fixtures_dir, "simple.jsonl", sid, "PostToolUse"), ENV, home)
    assert n > 0
    assert len(captured) == 1
    assert captured[0][0] == "https://ingest.test"
    assert state.load(sid, home)["offset"] > 0


def test_second_run_with_no_new_lines_sends_nothing(home, captured, fixtures_dir):
    sid = "11111111-1111-1111-1111-111111111111"
    p = _payload(fixtures_dir, "simple.jsonl", sid, "PostToolUse")
    exporter.run("PostToolUse", p, ENV, home)
    before = len(captured)
    assert exporter.run("PostToolUse", p, ENV, home) == 0
    assert len(captured) == before


def test_disabled_folder_exports_nothing(home, captured, fixtures_dir):
    sid = "11111111-1111-1111-1111-111111111111"
    p = _payload(fixtures_dir, "simple.jsonl", sid, "PostToolUse", cwd="/elsewhere")
    assert exporter.run("PostToolUse", p, ENV, home) == 0
    assert captured == []


def test_session_end_closes_the_root(home, captured, fixtures_dir):
    sid = "11111111-1111-1111-1111-111111111111"
    exporter.run("PostToolUse", _payload(fixtures_dir, "simple.jsonl", sid, "PostToolUse"), ENV, home)
    captured.clear()
    n = exporter.run("SessionEnd", _payload(fixtures_dir, "simple.jsonl", sid, "SessionEnd"), ENV, home)
    assert n >= 1          # at minimum, the final root span


def test_never_raises_on_a_missing_transcript(home, captured):
    p = {"session_id": "s9", "transcript_path": "/nope/missing.jsonl",
         "cwd": "/tmp/proj", "hook_event_name": "Stop"}
    assert exporter.run("Stop", p, ENV, home) == 0


def test_never_raises_on_a_garbage_payload(home, captured):
    assert exporter.run("Stop", {}, ENV, home) == 0


def test_lock_contention_is_a_no_op(home, captured, fixtures_dir):
    sid = "11111111-1111-1111-1111-111111111111"
    with state.session_lock(sid, home):
        n = exporter.run("PostToolUse",
                         _payload(fixtures_dir, "simple.jsonl", sid, "PostToolUse"), ENV, home)
    assert n == 0
    assert captured == []


def test_api_key_never_appears_in_the_log(home, captured, fixtures_dir):
    sid = "11111111-1111-1111-1111-111111111111"
    env = dict(ENV, RIUS_CLAUDE_DEBUG="true")
    exporter.run("PostToolUse", _payload(fixtures_dir, "simple.jsonl", sid, "PostToolUse"), env, home)
    import pathlib
    logs = list(pathlib.Path(home, ".claude", "rius", "log").glob("*.log"))
    assert logs, "expected a debug log file to be written when RIUS_CLAUDE_DEBUG=true"
    text = "\n".join(log.read_text() for log in logs)
    assert text.strip(), "expected the debug log to be non-empty"
    assert "glassflow_k" not in text
    assert config.redact("glassflow_k") in text


def test_spans_exported_counter_accumulates_on_success_only(home, captured, fixtures_dir):
    sid = "11111111-1111-1111-1111-111111111111"
    p = _payload(fixtures_dir, "simple.jsonl", sid, "PostToolUse")
    n1 = exporter.run("PostToolUse", p, ENV, home)
    st = state.load(sid, home)
    assert st["spans_exported"] == n1 > 0

    # SessionEnd on the same transcript produces further spans (e.g. the
    # closing root span) and a second successful export.
    n2 = exporter.run("SessionEnd", _payload(fixtures_dir, "simple.jsonl", sid, "SessionEnd"), ENV, home)
    st = state.load(sid, home)
    assert st["spans_exported"] == n1 + n2

    # A failed export must not increment the counter.
    before = st["spans_exported"]
    import exporter as exporter_mod
    exporter_mod.otlp.export = lambda ep, key, body, timeout=5.0: 500
    exporter.run("PostToolUse", _payload(fixtures_dir, "tool_call.jsonl", sid, "PostToolUse"), ENV, home)
    st = state.load(sid, home)
    assert st["spans_exported"] == before


def test_instance_id_is_minted_on_session_start_even_with_no_spans(home, captured):
    sid = "22222222-2222-2222-2222-222222222222"
    p = {"session_id": sid, "transcript_path": "/nope/missing.jsonl",
         "cwd": "/tmp/proj", "hook_event_name": "SessionStart"}
    exporter.run("SessionStart", p, ENV, home)
    st = state.load(sid, home)
    assert st.get("instance_id")
    assert captured == []


def test_instance_id_is_stable_across_invocations(home, captured, fixtures_dir):
    sid = "11111111-1111-1111-1111-111111111111"
    p = {"session_id": sid, "transcript_path": "/nope/missing.jsonl",
         "cwd": "/tmp/proj", "hook_event_name": "SessionStart"}
    exporter.run("SessionStart", p, ENV, home)
    first = state.load(sid, home)["instance_id"]

    exporter.run("PostToolUse", _payload(fixtures_dir, "simple.jsonl", sid, "PostToolUse"), ENV, home)
    second = state.load(sid, home)["instance_id"]
    assert first == second
