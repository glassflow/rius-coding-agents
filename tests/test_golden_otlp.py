"""Claude Code's OTLP is pinned: a fixed transcript run through the exporter
must carry exactly the data release 0.4.5 sent.

The golden files hold the protobuf bytes release 0.4.5 sent, generated from
origin/main before the agent profile refactor. Since RIUS-1237 the plugin sends
OTLP/JSON, so each JSON body is decoded into the real OTLP message and
re-serialised, and THAT must equal the golden protobuf bytes: the same spans,
field for field, across the wire-format change. Regenerate only for an intended
change in what is traced:
    RIUS_REGEN_GOLDEN=1 python -m pytest tests/test_golden_otlp.py
"""
import json
import os
import pathlib

import pytest

import exporter
from rius_cc import config
from tests import otlp_json, signed_in

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
    # No member email, so no `user.id`: these goldens are 0.4.5's bytes and
    # prove the spans it sent are unchanged. `user.id`, added later, has its
    # own tests (test_member_user_id.py) rather than a regenerated golden.
    signed_in.sign_in(str(h), email="")
    with open(config.path_rules_path(str(h)), "w") as fh:
        json.dump({"enabled_paths": ["/tmp"]}, fh)
    return str(h)


def _as_protobuf_hex(json_body):
    return otlp_json.to_request(json_body).SerializeToString(
        deterministic=True).hex()


def _export_bodies(home, transcript, session_id, monkeypatch):
    sent = []
    monkeypatch.setattr(exporter.otlp, "export",
                        lambda ep, key, body, timeout=5.0:
                        sent.append(_as_protobuf_hex(body)) or 200)
    monkeypatch.setattr(exporter, "_now_ns", lambda: NOW_NS)
    for event in EVENTS:
        payload = {"session_id": session_id, "transcript_path": transcript,
                   "cwd": "/tmp/proj", "hook_event_name": event}
        exporter.run(event, payload, ENV, home, "golden-instance")
    return sent


@pytest.mark.parametrize("name", sorted(CASES))
def test_claude_code_otlp_carries_the_same_data_as_0_4_5(name, home, fixtures_dir,
                                            monkeypatch):
    rel, session_id = CASES[name]
    bodies = _export_bodies(home, str(fixtures_dir / rel), session_id,
                            monkeypatch)
    golden = GOLDEN / (name + ".json")
    if os.environ.get("RIUS_REGEN_GOLDEN"):
        golden.write_text(json.dumps(bodies, indent=1) + "\n")
    assert bodies, "the exporter sent nothing"
    assert bodies == json.loads(golden.read_text())
