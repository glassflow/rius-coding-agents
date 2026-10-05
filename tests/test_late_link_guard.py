"""A conversation whose stop is found only by the LATE continuation check
still exports nothing.

exporter.run looks for a continued conversation twice: once before the
stopped check, and again once the transcript has entries (the copy may not
be there yet at the first look). If only the second look finds a
predecessor, and that conversation was stopped by a mid-session disable,
the stop arrives after the stopped check has already passed. Without the
guard right after the late check, the run falls through to the normal
export path with the folder's capture ON, and content read after the
disable leaves the machine.
"""
import json

import pytest

import exporter
from rius_cc import config, continuation, spans, state
from tests.signed_in import sign_in

OLD = "aaaaaaaa-0000-0000-0000-000000000001"
NEW = "bbbbbbbb-0000-0000-0000-000000000002"
ENV = {}
SECRET = "the plan we must not upload"


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    (h / ".claude" / "rius").mkdir(parents=True)
    sign_in(h)
    with open(config.path_rules_path(str(h)), "w") as fh:
        json.dump({"enabled_paths": ["/tmp"]}, fh)       # capture is ON here
    return str(h)


@pytest.fixture
def sent(monkeypatch):
    out = []

    def encode(resource, span_list):
        out.extend(span_list)
        return b"x"

    monkeypatch.setattr(exporter.otlp, "encode", encode)
    monkeypatch.setattr(exporter.otlp, "export",
                        lambda ep, key, body, timeout=5.0: 200)
    return out


def _setup(tmp_path, home, monkeypatch):
    proj = tmp_path / "proj"
    proj.mkdir()
    old_path = proj / (OLD + ".jsonl")
    old_path.write_text("")
    new_path = proj / (NEW + ".jsonl")
    rows = [
        {"type": "user", "uuid": "n1", "parentUuid": None, "promptId": "p1",
         "timestamp": "2026-09-30T10:06:00.000Z", "sessionId": NEW,
         "cwd": "/tmp/proj", "message": {"role": "user", "content": SECRET}},
        {"type": "assistant", "uuid": "n2", "parentUuid": "n1",
         "timestamp": "2026-09-30T10:06:02.000Z", "sessionId": NEW,
         "cwd": "/tmp/proj",
         "message": {"id": "msg_n", "role": "assistant", "model": "claude-opus-5",
                     "stop_reason": "end_turn",
                     "content": [{"type": "text", "text": SECRET + ", reply"}],
                     "usage": {"input_tokens": 2, "output_tokens": 9}}},
    ]
    new_path.write_text("".join(json.dumps(r) + "\n" for r in rows))

    # The old id was traced, then stopped by a mid-session disable.
    old = state.new_state()
    old.update(root_started=True, root_start_ns=1, content_stopped=True,
               instance_id="old-instance")
    state.save(OLD, home, old)

    calls = []

    def find(transcript_path, session_id):
        calls.append(session_id)
        if len(calls) == 1:
            return None                  # the early look: nothing yet
        return OLD, str(old_path), 0     # the late look finds the stopped one

    monkeypatch.setattr(continuation, "find_predecessor", find)
    return new_path, calls


def _hook(home, path, event):
    return exporter.run(event, {"session_id": NEW, "cwd": "/tmp/proj",
                                "transcript_path": str(path),
                                "hook_event_name": event}, ENV, home)


def test_a_stop_found_by_the_late_check_exports_nothing(
        tmp_path, home, sent, monkeypatch):
    new_path, calls = _setup(tmp_path, home, monkeypatch)
    _hook(home, new_path, "PostToolUse")
    assert len(calls) == 2, "the late check did not run"
    assert sent == [], "spans were exported after the conversation was stopped"
    st = state.load(NEW, home)
    assert st.get("content_stopped") is True
    assert st.get("conversation") == OLD
    assert state.load(OLD, home).get("handed_off_to") == NEW


def test_the_stopped_conversation_closes_without_content(
        tmp_path, home, sent, monkeypatch):
    new_path, _ = _setup(tmp_path, home, monkeypatch)
    _hook(home, new_path, "PostToolUse")
    _hook(home, new_path, "SessionEnd")
    root = spans.span_id_for("session:" + OLD)
    assert [s for s in sent if s.span_id == root and not s.pending]
    for s in sent:
        blob = repr(s.attributes) + (s.status_message or "")
        assert SECRET not in blob
        assert "input.value" not in s.attributes
        assert "output.value" not in s.attributes
