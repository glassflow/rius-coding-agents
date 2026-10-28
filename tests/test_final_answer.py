"""Claude Code writes a turn's final answer to the transcript a moment after
the Stop hook fires (RIUS-1704). The exporter for Stop waits for it, briefly,
so the turn's last model call goes out with the turn and not at the next
hook, minutes later or never."""
import json
import threading
import time

import pytest

import exporter
from rius_cc import _semconv_generated as semconv, final_answer, spans, state
from tests.test_exporter import ENV, captured, home  # noqa: F401

SID = "17041704-0000-0000-0000-000000000001"
FINAL_CALL = spans.span_id_for("msg_final")
TURN = spans.span_id_for("turn:p1")


def _line(**fields):
    entry = {"isSidechain": False, "sessionId": SID, "cwd": "/tmp/proj",
             "gitBranch": "main", "version": "2.1.312"}
    entry.update(fields)
    return json.dumps(entry) + "\n"


def _answer_line(uuid, parent, block, sidechain=False):
    return _line(type="assistant", uuid=uuid, parentUuid=parent,
                 isSidechain=sidechain, timestamp="2026-10-28T09:28:49.800Z",
                 message={"id": "msg_final", "role": "assistant",
                          "model": "claude-opus-5-5", "stop_reason": "end_turn",
                          "content": [block],
                          "usage": {"input_tokens": 3,
                                    "cache_read_input_tokens": 150000,
                                    "output_tokens": 40}})


PROMPT = _line(type="user", uuid="u1", parentUuid=None, promptId="p1",
               timestamp="2026-10-28T09:28:00.000Z",
               message={"role": "user", "content": [
                   {"type": "text", "text": "list the files"}]})
TOOL_CALL = _line(type="assistant", uuid="a1", parentUuid="u1",
                  timestamp="2026-10-28T09:28:10.000Z",
                  message={"id": "msg_tool", "role": "assistant",
                           "model": "claude-opus-5-5", "stop_reason": "tool_use",
                           "content": [{"type": "tool_use", "id": "toolu_1",
                                        "name": "Bash",
                                        "input": {"command": "ls"}}],
                           "usage": {"input_tokens": 5, "output_tokens": 20}})
TOOL_RESULT = _line(type="user", uuid="u2", parentUuid="a1", promptId="p1",
                    timestamp="2026-10-28T09:28:11.000Z",
                    message={"role": "user", "content": [
                        {"type": "tool_result", "tool_use_id": "toolu_1",
                         "content": "a\nb"}]})
ANSWER = (_answer_line("a2", "u2", {"type": "thinking", "thinking": "",
                                    "signature": "sig"})
          + _answer_line("a3", "a2", {"type": "text", "text": "a and b"}))
SUBAGENT_ANSWER = _answer_line("s1", "u2", {"type": "text", "text": "done"},
                               sidechain=True)
NEXT_PROMPT = _line(type="user", uuid="u3", parentUuid="a3", promptId="p2",
                    timestamp="2026-10-28T09:33:20.000Z",
                    message={"role": "user", "content": [
                        {"type": "text", "text": "thanks"}]})
UNANSWERED = PROMPT + TOOL_CALL + TOOL_RESULT


class FakeTime:
    def __init__(self, on_sleep=None):
        self.now = 0.0
        self.sleeps = 0
        self.on_sleep = on_sleep

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
        self.sleeps += 1
        if self.on_sleep:
            self.on_sleep(self.sleeps)


def _wait(path, offset=0, unanswered_before=True, fake=None):
    fake = fake or FakeTime()
    return final_answer.wait(str(path), offset, unanswered_before,
                             clock=fake.clock, sleep=fake.sleep), fake


@pytest.fixture
def budget(monkeypatch):
    monkeypatch.setattr(final_answer, "WAIT_S", 2.0)


def _append(path, text):
    with open(path, "a") as fh:
        fh.write(text)


def test_an_answer_already_written_is_not_waited_for(tmp_path, budget):
    path = tmp_path / "t.jsonl"
    path.write_text(UNANSWERED + ANSWER)
    found, fake = _wait(path)
    assert found and fake.sleeps == 0


def test_the_wait_ends_when_the_answer_lands(tmp_path, budget):
    path = tmp_path / "t.jsonl"
    path.write_text(UNANSWERED)
    fake = FakeTime(on_sleep=lambda n: n == 3 and _append(path, ANSWER))
    found, _ = _wait(path, fake=fake)
    assert found and fake.sleeps == 3


def test_an_answer_that_never_lands_is_waited_for_no_longer_than_the_budget(
        tmp_path, budget):
    path = tmp_path / "t.jsonl"
    path.write_text(UNANSWERED)
    found, fake = _wait(path)
    assert not found
    assert final_answer.WAIT_S <= fake.now < final_answer.WAIT_S + 2 * final_answer.POLL_S


def test_a_subagent_line_is_not_the_answer(tmp_path, budget):
    path = tmp_path / "t.jsonl"
    path.write_text(UNANSWERED + SUBAGENT_ANSWER)
    found, fake = _wait(path)
    assert not found and fake.sleeps > 0


def test_a_missing_transcript_is_not_waited_for(tmp_path, budget):
    found, fake = _wait(tmp_path / "missing.jsonl")
    assert not found and fake.sleeps == 0


@pytest.mark.parametrize("unanswered_before, waits", [(False, False),
                                                      (True, True)])
def test_with_nothing_new_what_was_read_before_decides(tmp_path, budget,
                                                       unanswered_before, waits):
    path = tmp_path / "t.jsonl"
    path.write_text(UNANSWERED)
    found, fake = _wait(path, offset=path.stat().st_size,
                        unanswered_before=unanswered_before)
    assert found is not waits
    assert (fake.sleeps > 0) is waits


def _payload(path, event):
    return {"session_id": SID, "transcript_path": str(path), "cwd": "/tmp/proj",
            "hook_event_name": event}


def _sent(captured):
    out = {}
    for _, _, body in captured:
        for span in json.loads(body)["resourceSpans"][0]["scopeSpans"][0]["spans"]:
            out[span["spanId"]] = span
    return out


def _finished(span):
    keys = {a["key"] for a in span.get("attributes", [])}
    return (semconv.RIUS_SPAN_PENDING not in keys
            and span.get("status", {}).get("code") == 1)


@pytest.mark.parametrize("read_before_stop", [False, True])
def test_stop_sends_a_final_answer_written_just_after_it(
        home, captured, tmp_path, budget, read_before_stop):
    path = tmp_path / "t.jsonl"
    path.write_text(UNANSWERED)
    if read_before_stop:
        exporter.run("PostToolUse", _payload(path, "PostToolUse"), ENV, home)
        captured.clear()

    writer = threading.Timer(0.3, _append, (path, ANSWER))
    writer.start()
    exporter.run("Stop", _payload(path, "Stop"), ENV, home)
    writer.join()

    sent = _sent(captured)
    assert sent[FINAL_CALL]["parentSpanId"] == TURN
    assert _finished(sent[TURN])
    assert int(sent[TURN]["endTimeUnixNano"]) >= int(sent[FINAL_CALL]["endTimeUnixNano"])

    captured.clear()
    _append(path, NEXT_PROMPT)
    exporter.run("UserPromptSubmit", _payload(path, "UserPromptSubmit"), ENV, home)
    resent = _sent(captured)
    assert spans.span_id_for("turn:p2") in resent
    assert FINAL_CALL not in resent


def test_a_stop_whose_answer_never_comes_ends_as_before(
        home, captured, tmp_path, monkeypatch):
    monkeypatch.setattr(final_answer, "WAIT_S", 0.3)
    path = tmp_path / "t.jsonl"
    path.write_text(UNANSWERED)

    started = time.monotonic()
    exporter.run("Stop", _payload(path, "Stop"), ENV, home)
    assert 0.3 <= time.monotonic() - started < 2.0
    sent = _sent(captured)
    assert _finished(sent[TURN])
    assert FINAL_CALL not in sent

    captured.clear()
    _append(path, ANSWER + NEXT_PROMPT)
    exporter.run("UserPromptSubmit", _payload(path, "UserPromptSubmit"), ENV, home)
    assert FINAL_CALL in _sent(captured)


@pytest.mark.parametrize("read_before_stop", [False, True])
def test_stop_does_not_wait_for_an_answer_already_read_or_written(
        home, captured, tmp_path, monkeypatch, read_before_stop):
    monkeypatch.setattr(final_answer, "WAIT_S", 30.0)
    path = tmp_path / "t.jsonl"
    path.write_text(UNANSWERED + ANSWER)
    if read_before_stop:
        exporter.run("PostToolUse", _payload(path, "PostToolUse"), ENV, home)
        captured.clear()

    started = time.monotonic()
    exporter.run("Stop", _payload(path, "Stop"), ENV, home)
    assert time.monotonic() - started < 5.0
    assert _finished(_sent(captured)[TURN])


def test_session_end_behind_a_stop_still_waiting_closes_the_trace(
        home, tmp_path, monkeypatch, budget):
    """A user who quits right after the answer: the SessionEnd exporter
    queues behind a Stop that holds the lock for the whole wait and then a
    slow POST, and must still get the lock, or the trace never closes."""
    def slow_export(ep, key, body, timeout=5.0):
        time.sleep(0.5)
        return 200

    monkeypatch.setattr(exporter.otlp, "export", slow_export)
    path = tmp_path / "t.jsonl"
    path.write_text(UNANSWERED)
    stop = threading.Thread(target=exporter.run,
                            args=("Stop", _payload(path, "Stop"), ENV, home))
    stop.start()
    time.sleep(0.2)
    exporter.run("SessionEnd", _payload(path, "SessionEnd"), ENV, home)
    stop.join()
    assert state.load(SID, home).get("finalized")
