"""Claude Code's OTLP bytes are pinned: a fixed transcript run through the
exporter must encode exactly what release 0.4.5 encoded.

The golden files were generated from origin/main (0.4.5) before the agent
profile refactor. Regenerate only for an intended wire change:
    RIUS_REGEN_GOLDEN=1 python -m pytest tests/test_golden_otlp.py
"""
import json
import os
import pathlib

import pytest

import exporter
from rius_cc import config
from tests import signed_in

GOLDEN = pathlib.Path(__file__).parent / "fixtures" / "golden"
ENV = {}
EVENTS = ("SessionStart", "PostToolUse", "Stop", "SessionEnd")
NOW_NS = 1790000000000000000
CASES = {
    "subagent": ("subagent_files/55555555-5555-5555-5555-555555555555.jsonl",
                 "55555555-5555-5555-5555-555555555555"),
    "tool_errors_bash": ("tool_errors_bash.jsonl",
                         "66666666-6666-6666-6666-666666666666"),
    "system_turns": ("system_turns.jsonl",
                     "77777777-7777-7777-7777-777777777777"),
}


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    (h / ".claude" / "rius").mkdir(parents=True)
    signed_in.sign_in(str(h))
    with open(config.path_rules_path(str(h)), "w") as fh:
        json.dump({"enabled_paths": ["/tmp"]}, fh)
    return str(h)


def _export_bodies(home, transcript, session_id, monkeypatch):
    sent = []
    monkeypatch.setattr(exporter.otlp, "export",
                        lambda ep, key, body, timeout=5.0:
                        sent.append(body.hex()) or 200)
    monkeypatch.setattr(exporter, "_now_ns", lambda: NOW_NS)
    for event in EVENTS:
        payload = {"session_id": session_id, "transcript_path": transcript,
                   "cwd": "/tmp/proj", "hook_event_name": event}
        exporter.run(event, payload, ENV, home, "golden-instance")
    return sent


@pytest.mark.parametrize("name", sorted(CASES))
def test_claude_code_otlp_is_byte_identical(name, home, fixtures_dir,
                                            monkeypatch):
    rel, session_id = CASES[name]
    bodies = _export_bodies(home, str(fixtures_dir / rel), session_id,
                            monkeypatch)
    golden = GOLDEN / (name + ".json")
    if os.environ.get("RIUS_REGEN_GOLDEN"):
        golden.write_text(json.dumps(bodies, indent=1) + "\n")
    assert bodies, "the exporter sent nothing"
    assert bodies == json.loads(golden.read_text())
