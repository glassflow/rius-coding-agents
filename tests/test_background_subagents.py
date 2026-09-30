"""A background subagent's work lands AFTER its Agent tool call returns.

Claude Code 2.1.x runs subagents in the background (meta.json says
`"requestShape": "background"`): the Agent tool_result is a launch
acknowledgement that arrives within a second, while the subagent's own
transcript is still just its brief. The subagent then works for a minute or
more, and the main session hears about it later through a
<task-notification> turn.

Closing the subagent at that tool_result stopped its file being read at the
brief: a real session's three subagents made 21 model calls that never
reached the trace, and their AGENT spans ended at the acknowledgement (10-24
s) instead of when the subagents finished (26-75 s).
"""
import json

from rius_cc import spans, state, subagents, transcript

SID = "88888888-8888-8888-8888-888888888888"
AGENT = "agent-bg1"
BASE = {"isSidechain": False, "sessionId": SID, "cwd": "/tmp/proj",
        "gitBranch": "main", "version": "2.1.284"}


def _line(**kw):
    row = dict(BASE)
    row.update(kw)
    return json.dumps(row) + "\n"


def _ts(seconds):
    return "2026-09-30T10:00:%06.3fZ" % seconds


def _assistant(uuid, t, mid, content, out_tokens, stop, sidechain=False):
    return _line(type="assistant", uuid=uuid, parentUuid=None,
                 timestamp=_ts(t), isSidechain=sidechain,
                 message={"id": mid, "role": "assistant",
                          "model": "claude-haiku-4-5", "stop_reason": stop,
                          "content": content,
                          "usage": {"input_tokens": 3,
                                    "output_tokens": out_tokens,
                                    "cache_read_input_tokens": 100,
                                    "cache_creation_input_tokens": 10}})


def _user(uuid, t, content, sidechain=False, prompt="p1"):
    return _line(type="user", uuid=uuid, parentUuid=None, timestamp=_ts(t),
                 promptId=prompt, isSidechain=sidechain,
                 message={"role": "user", "content": content})


def _result(tool_id, text):
    return [{"type": "tool_result", "tool_use_id": tool_id,
             "is_error": False, "content": text}]


class Session:
    """Files on disk that grow, and a hook that reads whatever is new."""

    def __init__(self, tmp_path):
        self.main = tmp_path / (SID + ".jsonl")
        self.subdir = tmp_path / SID / "subagents"
        self.subdir.mkdir(parents=True)
        self.sub = self.subdir / (AGENT + ".jsonl")
        (self.subdir / (AGENT + ".meta.json")).write_text(json.dumps({
            "agentType": "general-purpose", "description": "count words",
            "toolUseId": "toolu_bg", "spawnDepth": 1,
            "requestShape": "background", "model": "haiku"}))
        self.main.write_text("")
        self.sub.write_text("")
        self.st = state.new_state()
        self.ctx = spans.Ctx(session_id=SID, cwd="/tmp/proj",
                             git_branch="main", cc_version="2.1.284",
                             service_name="claude-code", capture_content=True,
                             max_attr_bytes=32768)
        self.out = []

    def append(self, path, *lines):
        with open(path, "a") as fh:
            fh.write("".join(lines))

    def hook(self):
        entries, new_offset = transcript.read_from(str(self.main),
                                                   self.st["offset"])
        self.out += spans.build(entries, self.st, self.ctx,
                                source_path=str(self.main))
        self.st["offset"] = new_offset
        self.out += subagents.expand(self.st, self.ctx, str(self.subdir))


def _run(tmp_path):
    s = Session(tmp_path)
    # 10:00:00 prompt; 10:00:01 the Agent call; the subagent file holds its brief.
    s.append(s.main,
             _user("u1", 0, "count the words in parallel"),
             _assistant("a1", 1, "msg_main1",
                        [{"type": "tool_use", "id": "toolu_bg", "name": "Agent",
                          "input": {"prompt": "count words",
                                    "run_in_background": True}}],
                        40, "tool_use"))
    s.append(s.sub, _user("s1", 1.5, "count words", sidechain=True))
    s.hook()
    # 10:00:02 the launch acknowledgement: the tool call is over, the
    # subagent has not made a single model call yet.
    s.append(s.main, _user("u2", 2, _result("toolu_bg", "Async agent launched")))
    s.hook()
    # The subagent works until 10:00:30.
    s.append(s.sub,
             _assistant("s2", 10, "msg_sub1",
                        [{"type": "tool_use", "id": "toolu_s1", "name": "Bash",
                          "input": {"command": "wc -w notes.txt"}}],
                        25, "tool_use", sidechain=True),
             _user("s3", 12, _result("toolu_s1", "42 notes.txt"), sidechain=True),
             _assistant("s4", 30, "msg_sub2",
                        [{"type": "text", "text": "There are 42 words."}],
                        15, "end_turn", sidechain=True))
    # 10:00:31 the main session is told the subagent finished.
    s.append(s.main, _user("u3", 31, "<task-notification>done</task-notification>",
                           prompt="p2"))
    s.hook()
    return s


def _latest(out):
    latest = {}
    for span in out:
        if span.pending and span.span_id in latest:
            continue
        latest[span.span_id] = span
    return latest


def test_background_subagent_model_calls_are_traced(tmp_path):
    s = _run(tmp_path)
    agent_span_id = spans.span_id_for("subagent:" + AGENT)
    latest = _latest(s.out)
    sub_llm = [x for x in latest.values()
               if x.kind_oi == "LLM" and x.parent_span_id == agent_span_id]
    assert len(sub_llm) == 2
    assert sum(x.attributes["gen_ai.usage.output_tokens"] for x in sub_llm) == 40
    bash = latest[spans.span_id_for("toolu_s1")]
    assert bash.pending is False
    assert bash.parent_span_id in {x.span_id for x in sub_llm}


def test_background_subagent_span_ends_when_its_work_ends(tmp_path):
    s = _run(tmp_path)
    agent_span_id = spans.span_id_for("subagent:" + AGENT)
    copies = [x for x in s.out if x.span_id == agent_span_id]
    assert len({x.start_ns for x in copies}) == 1, "start must not move"
    final = _latest(s.out)[agent_span_id]
    assert final.pending is False
    assert final.attributes["gen_ai.agent.name"] == "general-purpose"
    # From the subagent's first line (10:00:01.5) to its last (10:00:30),
    # not to the launch acknowledgement (10:00:02).
    assert final.end_ns - final.start_ns == 28_500_000_000


def test_a_subagent_starts_when_its_own_transcript_does(tmp_path):
    """Claude Code timestamps a tool_use line when the block has streamed,
    and a parallel batch of Agent calls runs only once the whole response
    has: in the real session a subagent's file began 24 s after its Agent
    tool_use line. The subagent ran from its file's first line."""
    s = _run(tmp_path)
    final = _latest(s.out)[spans.span_id_for("subagent:" + AGENT)]
    assert final.start_ns == transcript._timestamp_ns(_ts(1.5))


def test_the_agent_tool_span_itself_still_ends_at_its_result(tmp_path):
    """The TOOL span is the call, which really did return at 10:00:02."""
    s = _run(tmp_path)
    tool = _latest(s.out)[spans.span_id_for("toolu_bg")]
    assert tool.pending is False
    assert tool.end_ns - tool.start_ns == 1_000_000_000


def test_a_finished_subagent_is_not_re_emitted_when_nothing_changed(tmp_path):
    s = _run(tmp_path)
    before = len(s.out)
    s.hook()
    s.hook()
    assert len(s.out) == before


def test_a_subagent_whose_file_is_still_empty_starts_at_its_first_line(tmp_path):
    """meta.json can exist before the subagent has written a line; the span
    must not be pinned to the Agent tool_use time because of that."""
    s = Session(tmp_path)
    s.append(s.main,
             _user("u1", 0, "count the words in parallel"),
             _assistant("a1", 1, "msg_main1",
                        [{"type": "tool_use", "id": "toolu_bg", "name": "Agent",
                          "input": {"prompt": "count words",
                                    "run_in_background": True}}],
                        40, "tool_use"))
    s.hook()                                   # meta.json there, file empty
    s.append(s.sub, _user("s1", 5, "count words", sidechain=True))
    s.append(s.main, _user("u2", 6, _result("toolu_bg", "Async agent launched")))
    s.hook()
    s.append(s.sub, _assistant("s2", 9, "msg_sub1",
                               [{"type": "text", "text": "done"}], 5,
                               "end_turn", sidechain=True))
    s.hook()
    agent_span_id = spans.span_id_for("subagent:" + AGENT)
    copies = [x for x in s.out if x.span_id == agent_span_id]
    assert {x.start_ns for x in copies} == {transcript._timestamp_ns(_ts(5))}
    assert _latest(s.out)[agent_span_id].end_ns == transcript._timestamp_ns(_ts(9))
