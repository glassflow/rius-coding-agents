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
    for log in logs:
        assert "glassflow_k" not in log.read_text()
