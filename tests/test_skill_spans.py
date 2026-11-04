"""A Skill call's execute_tool span says which Agent Skill it loaded, with
the OTel GenAI conventions' `gen_ai.skill.name`, and where the skill
resolved from, with `gen_ai.skill.source.uri`.

The name is the one Claude Code resolved, which it writes on the result
entry as `toolUseResult.commandName`; an older entry without one falls back
to the tool's `skill` input. A call that failed (an unknown skill, a denial)
loaded nothing and names none. The folder is what Claude Code writes in the
meta entry that follows the tool_result ("Base directory for this skill:
..."), named by `sourceToolUseID`; the fixture keeps the shape of real
Claude Code transcripts, with the skill bodies cut. Nothing is invented when
Claude Code does not say: a built-in skill unpacked to a temporary folder,
or a skill that writes no folder, gets no source URI.
"""
import json
import pathlib
import warnings

import pytest

from rius_cc import spans, state, subagents, transcript
from rius_cc import _semconv_generated as semconv

FIXTURE = "skill_call.jsonl"
PLUGIN_DIR = ("/Users/dev/.claude/plugins/cache/claude-plugins-official/"
              "superpowers/7.0.0/skills/systematic-debugging")


def _ctx(capture):
    return spans.Ctx(session_id="88888888-8888-8888-8888-888888888888",
                     cwd="/tmp/proj", git_branch="main", cc_version="2.1.303",
                     service_name="claude-code", capture_content=capture,
                     max_attr_bytes=32768)


def _state():
    return {"offset": 0, "open_tools": {}, "open_turns": {},
            "root_started": False, "root_start_ns": 0, "last_ns": 0,
            "open_task_spans": []}


def _skills(fixtures_dir, capture=True):
    path = str(fixtures_dir / FIXTURE)
    entries, _ = transcript.read_from(path, 0)
    out = spans.build(entries, _state(), _ctx(capture), source_path=path)
    return {s.attributes[semconv.GEN_AI_TOOL_CALL_ID]: s for s in out
            if s.kind_oi == "TOOL" and not s.pending}


def test_a_skill_call_names_the_skill_it_loaded(fixtures_dir):
    tools = _skills(fixtures_dir)
    assert {k: s.attributes.get(semconv.GEN_AI_SKILL_NAME) for k, s in tools.items()} == {
        "toolu_plugin": "superpowers:systematic-debugging",
        "toolu_user": "browser-navigation",
        "toolu_bundled": "dataviz",
        "toolu_prompt": "design-lead",
        "toolu_old": "superpowers:brainstorming",
        "toolu_unknown": None,
        "toolu_denied": None,
    }
    assert all(s.attributes[semconv.GEN_AI_TOOL_NAME] == "Skill"
               for s in tools.values())


@pytest.mark.parametrize("call_id", ["toolu_unknown", "toolu_denied"])
def test_a_skill_call_that_failed_names_no_skill(fixtures_dir, call_id):
    """"Unknown skill: ..." or a denied call: nothing loaded, so the name the
    model asked for is not sent as the skill, with capture on or off."""
    for capture in (True, False):
        span = _skills(fixtures_dir, capture)[call_id]
        assert span.status_code == "ERROR"
        assert semconv.GEN_AI_SKILL_NAME not in span.attributes
        assert semconv.GEN_AI_SKILL_SOURCE_URI not in span.attributes


def _one_skill_call(tmp_path, skill, result_extra):
    """A transcript with one Skill call, whose result entry carries
    `result_extra`: the entry fields Claude Code writes beside the result."""
    lines = [
        {"type": "user", "uuid": "u1", "timestamp": "2026-10-19T14:55:00.000Z",
         "sessionId": "88888888-8888-8888-8888-888888888888", "promptId": "p1",
         "message": {"role": "user", "content": [{"type": "text", "text": "go"}]}},
        {"type": "assistant", "uuid": "a1", "timestamp": "2026-10-19T14:55:01.000Z",
         "sessionId": "88888888-8888-8888-8888-888888888888",
         "message": {"id": "m1", "role": "assistant", "content": [
             {"type": "tool_use", "id": "toolu_s", "name": "Skill",
              "input": {"skill": skill}}]}},
        dict({"type": "user", "uuid": "r1", "timestamp": "2026-10-19T14:55:02.000Z",
              "sessionId": "88888888-8888-8888-8888-888888888888", "promptId": "p1",
              "message": {"role": "user", "content": [
                  {"type": "tool_result", "tool_use_id": "toolu_s",
                   "content": "Launching skill"}]}}, **result_extra),
    ]
    path = tmp_path / "t.jsonl"
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))
    entries, _ = transcript.read_from(str(path), 0)
    out = spans.build(entries, _state(), _ctx(False), source_path=str(path))
    return next(s for s in out if s.kind_oi == "TOOL" and not s.pending)


def test_the_name_claude_code_resolved_comes_first(tmp_path, monkeypatch):
    """With `commandName` on the result, that is the name, and the
    transcript is not read again for the input's."""
    monkeypatch.setattr(spans, "_skill_name_at", lambda *a: pytest.fail("re-read"))
    span = _one_skill_call(tmp_path, "brainstorm", {
        "toolUseResult": {"success": True, "commandName": "superpowers:brainstorm"}})
    assert span.attributes[semconv.GEN_AI_SKILL_NAME] == "superpowers:brainstorm"


def test_without_a_resolved_name_the_input_is_read_back(tmp_path):
    span = _one_skill_call(tmp_path, "superpowers:brainstorm", {})
    assert span.attributes[semconv.GEN_AI_SKILL_NAME] == "superpowers:brainstorm"


def test_a_resolved_name_that_is_not_a_skill_name_is_not_kept(tmp_path):
    span = _one_skill_call(tmp_path, "superpowers:brainstorm", {
        "toolUseResult": {"success": True, "commandName": "../etc/passwd"}})
    assert semconv.GEN_AI_SKILL_NAME not in span.attributes


SUB_SID = "7a7a7a7a-0000-4000-8000-000000000072"


def test_a_subagent_skill_call_names_its_skill(fixtures_dir):
    """A subagent's calls are in its own file: the re-read for a result
    without `commandName` reads that file, not the main transcript."""
    path = str(fixtures_dir / "skill_subagent" / (SUB_SID + ".jsonl"))
    st = state.new_state()
    ctx = spans.Ctx(session_id=SUB_SID, cwd="/tmp/proj", git_branch="main",
                    cc_version="2.1.303", service_name="claude-code",
                    capture_content=False, max_attr_bytes=32768)
    entries, _ = transcript.read_from(path, 0)
    out = spans.build(entries, st, ctx, source_path=path)
    out += subagents.expand(st, ctx, subagents.dir_for(path, SUB_SID))
    names = {s.attributes[semconv.GEN_AI_TOOL_CALL_ID]: s.attributes.get(semconv.GEN_AI_SKILL_NAME)
             for s in out if s.name == "Skill" and not s.pending}
    assert names == {"toolu_sub_skill": "superpowers:brainstorming",
                     "toolu_sub_old": "dataviz"}


def test_the_source_is_the_folder_claude_code_loaded_it_from(fixtures_dir):
    tools = _skills(fixtures_dir)
    assert tools["toolu_plugin"].attributes[semconv.GEN_AI_SKILL_SOURCE_URI] == \
        "file://" + PLUGIN_DIR
    assert tools["toolu_user"].attributes[semconv.GEN_AI_SKILL_SOURCE_URI] == \
        "file:///Users/dev/.claude/skills/browser-navigation"


def test_no_source_is_invented(fixtures_dir):
    """A built-in skill's folder is a temporary copy, which the conventions
    say a source URI must not name; a skill that writes no folder has none."""
    tools = _skills(fixtures_dir)
    assert semconv.GEN_AI_SKILL_SOURCE_URI not in tools["toolu_bundled"].attributes
    assert semconv.GEN_AI_SKILL_SOURCE_URI not in tools["toolu_prompt"].attributes


def test_the_name_is_sent_with_capture_off_and_the_folder_is_not(fixtures_dir):
    tools = _skills(fixtures_dir, capture=False)
    plugin = tools["toolu_plugin"].attributes
    assert plugin[semconv.GEN_AI_SKILL_NAME] == "superpowers:systematic-debugging"
    assert semconv.GEN_AI_SKILL_SOURCE_URI not in plugin


def test_a_folder_entry_a_later_read_brings_is_not_joined(fixtures_dir):
    """The span closes at the tool_result. A folder entry read after that
    batch is not joined to it: the span keeps its name and has no source."""
    path = str(fixtures_dir / FIXTURE)
    entries, _ = transcript.read_from(path, 0)
    cut = next(i for i, e in enumerate(entries)
               if e.raw.get("sourceToolUseID") == "toolu_plugin")
    st, ctx = _state(), _ctx(True)
    first = spans.build(entries[:cut], st, ctx, source_path=path)
    second = spans.build(entries[cut:], st, ctx, source_path=path)
    plugin = [s for s in first + second if not s.pending
              and s.attributes.get(semconv.GEN_AI_TOOL_CALL_ID) == "toolu_plugin"]
    assert len(plugin) == 1
    assert plugin[0].attributes[semconv.GEN_AI_SKILL_NAME] == "superpowers:systematic-debugging"
    assert semconv.GEN_AI_SKILL_SOURCE_URI not in plugin[0].attributes


def test_the_skill_body_is_not_a_turn(fixtures_dir):
    path = str(fixtures_dir / FIXTURE)
    entries, _ = transcript.read_from(path, 0)
    out = spans.build(entries, _state(), _ctx(False), source_path=path)
    assert len([s for s in out if s.name == "turn"]) == 1


def test_the_name_is_read_back_from_the_transcript_not_kept(fixtures_dir):
    """A Skill call's tool_use and tool_result arrive in different hook
    reads. Between them the state keeps where the line is, not the name."""
    path = str(fixtures_dir / FIXTURE)
    entries, _ = transcript.read_from(path, 0)
    use = next(i for i, e in enumerate(entries) if e.tool_uses())
    st, ctx = _state(), _ctx(False)
    spans.build(entries[:use + 1], st, ctx, source_path=path)
    assert "systematic-debugging" not in json.dumps(st)
    out = spans.build(entries[use + 1:], st, ctx, source_path=path)
    plugin = [s for s in out if not s.pending
              and s.attributes.get(semconv.GEN_AI_TOOL_CALL_ID) == "toolu_plugin"]
    assert plugin[0].attributes[semconv.GEN_AI_SKILL_NAME] == "superpowers:systematic-debugging"


def test_a_line_that_is_not_the_call_names_no_skill(fixtures_dir):
    """A transcript rewritten under the offset: the line there is some other
    entry, and no name is taken from it."""
    path = str(fixtures_dir / FIXTURE)
    assert spans._skill_name_at(path, 0, "toolu_plugin") == ""
    assert spans._skill_name_at(path, -1, "toolu_plugin") == ""
    assert spans._skill_name_at("", 0, "toolu_plugin") == ""


@pytest.mark.parametrize("name", [
    "", " ", "a" * 200, "x\ny", "../etc/passwd", "sk-ant-api03-key with spaces",
    {"nested": "dict"}, 7, None])
def test_a_name_that_is_not_a_skill_name_is_not_kept(name):
    assert spans._skill_name({"skill": name}) == ""


@pytest.mark.parametrize("path,uri", [
    ("/opt/skills/code review", "file:///opt/skills/code%20review"),
    ("C:\\Users\\dev\\.claude\\skills\\x", "file:///C:/Users/dev/.claude/skills/x"),
    ("relative/skills/x", ""),
    ("/private/tmp/claude-501/bundled-skills/2.1.304/abc/dataviz", ""),
])
def test_a_folder_becomes_a_file_uri(path, uri):
    assert spans._skill_source_uri(path) == uri


@pytest.mark.parametrize("path", [
    pathlib.PurePosixPath("/opt/skills/ünïcode %#?"),
    pathlib.PureWindowsPath("C:\\Users\\dev\\a b"),
    pathlib.PureWindowsPath("\\\\server\\share\\a b"),
])
def test_file_uri_equals_as_uri(path):
    """Python 3.14 deprecates `PurePath.as_uri()`; the URI built in its
    place is the same, percent-encoding and all."""
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        uri = spans._file_uri(path)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        assert uri == path.as_uri()
