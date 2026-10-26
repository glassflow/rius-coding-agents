"""A Skill call's execute_tool span says which Agent Skill it loaded, with
the OTel GenAI conventions' `gen_ai.skill.name`, and where the skill
resolved from, with `gen_ai.skill.source.uri`.

The name is the tool's `skill` input. The folder is what Claude Code writes
in the meta entry that follows the tool_result ("Base directory for this
skill: ..."), named by `sourceToolUseID`; the fixture keeps the shape of
real Claude Code 2.1.303 transcripts, with the skill bodies cut. Nothing
is invented when Claude Code does not say: a built-in skill unpacked to a
temporary folder, or a skill that writes no folder, gets no source URI.
"""
import json

import pytest

from rius_cc import spans, transcript
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
    assert {k: s.attributes[spans.SKILL_NAME] for k, s in tools.items()} == {
        "toolu_plugin": "superpowers:systematic-debugging",
        "toolu_user": "browser-navigation",
        "toolu_bundled": "dataviz",
        "toolu_prompt": "design-lead",
    }
    assert all(s.attributes[semconv.GEN_AI_TOOL_NAME] == "Skill"
               for s in tools.values())


def test_the_source_is_the_folder_claude_code_loaded_it_from(fixtures_dir):
    tools = _skills(fixtures_dir)
    assert tools["toolu_plugin"].attributes[spans.SKILL_SOURCE_URI] == \
        "file://" + PLUGIN_DIR
    assert tools["toolu_user"].attributes[spans.SKILL_SOURCE_URI] == \
        "file:///Users/dev/.claude/skills/browser-navigation"


def test_no_source_is_invented(fixtures_dir):
    """A built-in skill's folder is a temporary copy, which the conventions
    say a source URI must not name; a skill that writes no folder has none."""
    tools = _skills(fixtures_dir)
    assert spans.SKILL_SOURCE_URI not in tools["toolu_bundled"].attributes
    assert spans.SKILL_SOURCE_URI not in tools["toolu_prompt"].attributes


def test_the_name_is_sent_with_capture_off_and_the_folder_is_not(fixtures_dir):
    tools = _skills(fixtures_dir, capture=False)
    plugin = tools["toolu_plugin"].attributes
    assert plugin[spans.SKILL_NAME] == "superpowers:systematic-debugging"
    assert spans.SKILL_SOURCE_URI not in plugin


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
    assert plugin[0].attributes[spans.SKILL_NAME] == "superpowers:systematic-debugging"
    assert spans.SKILL_SOURCE_URI not in plugin[0].attributes


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
    assert plugin[0].attributes[spans.SKILL_NAME] == "superpowers:systematic-debugging"


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
