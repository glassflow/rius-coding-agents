from rius_cc import transcript


def test_skips_non_conversational_lines(fixtures_dir):
    entries, _ = transcript.read_from(str(fixtures_dir / "simple.jsonl"), 0)
    assert [e.kind for e in entries] == ["user", "assistant"]


def test_parses_entry_scalars(fixtures_dir):
    entries, _ = transcript.read_from(str(fixtures_dir / "simple.jsonl"), 0)
    user, asst = entries
    assert user.uuid == "u1"
    assert user.parent_uuid is None
    assert user.prompt_id == "p1"
    assert user.cwd == "/tmp/proj"
    assert user.git_branch == "main"
    assert user.cc_version == "2.1.278"
    assert asst.parent_uuid == "u1"
    assert asst.message["model"] == "claude-opus-5"


def test_timestamp_is_integer_nanoseconds(fixtures_dir):
    entries, _ = transcript.read_from(str(fixtures_dir / "simple.jsonl"), 0)
    assert entries[0].timestamp_ns == 1790071200000000000
    # .500Z must survive as exactly half a second, not a float artefact
    assert entries[1].timestamp_ns - entries[0].timestamp_ns == 2_500_000_000


def test_offset_advances_to_end_of_file(fixtures_dir, tmp_path):
    src = (fixtures_dir / "simple.jsonl").read_bytes()
    p = tmp_path / "t.jsonl"
    p.write_bytes(src)
    entries, offset = transcript.read_from(str(p), 0)
    assert offset == len(src)
    more, offset2 = transcript.read_from(str(p), offset)
    assert more == [] and offset2 == offset


def test_trailing_partial_line_is_not_consumed(tmp_path, fixtures_dir):
    src = (fixtures_dir / "simple.jsonl").read_bytes()
    partial = src + b'{"type":"user","uuid":"u9"'
    p = tmp_path / "t.jsonl"
    p.write_bytes(partial)
    entries, offset = transcript.read_from(str(p), 0)
    assert offset == len(src)          # stopped before the partial line
    assert all(e.uuid != "u9" for e in entries)


def test_malformed_line_is_skipped_not_raised(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_bytes(b'{"broken\n{"type":"mode"}\n')
    entries, offset = transcript.read_from(str(p), 0)
    assert entries == []
    assert offset == p.stat().st_size


def test_tool_use_and_result_blocks(fixtures_dir):
    entries, _ = transcript.read_from(str(fixtures_dir / "tool_call.jsonl"), 0)
    asst = entries[1]
    uses = asst.tool_uses()
    assert len(uses) == 1
    assert uses[0]["name"] == "Read"
    assert uses[0]["id"] == "toolu_1"
    results = entries[2].tool_results()
    assert results[0]["tool_use_id"] == "toolu_1"
    assert results[0]["is_error"] is False


def test_text_concatenates_text_blocks(fixtures_dir):
    entries, _ = transcript.read_from(str(fixtures_dir / "simple.jsonl"), 0)
    assert entries[1].text() == "hi there"
