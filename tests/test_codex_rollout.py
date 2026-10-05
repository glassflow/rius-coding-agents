"""Codex rollout JSONL -> neutral records.

The fixture is a real Codex 0.144.1 rollout, recorded against a local mock
of the Responses API: one turn with a failing command, a passing command and
an MCP call its server marked as an error, then two resumed turns. Codex's
base instructions and developer prompt are trimmed from it.
"""
import json

from rius_cc import codex_rollout as cr

FIXTURE = "codex/mock_tools_mcp_resume.jsonl"


def _records(fixtures_dir):
    records, _ = cr.read_from(str(fixtures_dir / FIXTURE), 0)
    return records


def _kinds(records, kind):
    return [r for r in records if r.kind == kind]


def test_reads_the_session_and_every_turn(fixtures_dir):
    records = _records(fixtures_dir)
    session = _kinds(records, cr.SESSION)[0]
    assert session.get("thread_id") == "01a10b40-afd3-7d51-a63c-9d2d01e8445e"
    assert session.get("cli_version") == "0.144.1"
    assert len(_kinds(records, cr.TURN_START)) == 3
    assert len(_kinds(records, cr.TURN_END)) == 3
    assert {r.get("model") for r in _kinds(records, cr.TURN_CONTEXT)} == {"mock-model"}


def test_token_counts_are_the_last_call_not_the_running_total(fixtures_dir):
    usage = _kinds(_records(fixtures_dir), cr.USAGE)
    assert [u.get("input_tokens") for u in usage] == [6000, 7000, 8000, 9000, 10000, 11000]
    assert usage[0].get("cached_input_tokens") == 2500
    assert usage[0].get("reasoning_output_tokens") == 5
    assert usage[0].get("context_window") == 258400


def test_an_mcp_call_is_named_by_its_namespace(fixtures_dir):
    calls = _kinds(_records(fixtures_dir), cr.TOOL_CALL)
    assert [c.get("name") for c in calls] == \
        ["exec_command", "exec_command", "mcp__t3echo__echo"]
    result = _kinds(_records(fixtures_dir), cr.MCP_RESULT)[0]
    assert result.get("call_id") == calls[2].get("call_id")
    assert result.get("is_error") is True


def test_injected_context_is_not_a_record(fixtures_dir):
    texts = [r.get("text") for r in _kinds(_records(fixtures_dir), cr.USER_MESSAGE)]
    assert texts == ["tools please", "just chat", "slow please"]


def test_a_partial_last_line_is_left_for_the_next_read(tmp_path, fixtures_dir):
    full = (fixtures_dir / FIXTURE).read_bytes()
    lines = full.splitlines(keepends=True)
    path = tmp_path / "rollout.jsonl"
    path.write_bytes(b"".join(lines[:10]) + lines[10][:25])

    first, offset = cr.read_from(str(path), 0)
    assert offset == len(b"".join(lines[:10]))

    path.write_bytes(full)
    rest, end = cr.read_from(str(path), offset)
    assert end == len(full)
    assert len(first) + len(rest) == len(_records(fixtures_dir))
    assert rest[0].offset == offset


def test_a_rate_limit_only_token_count_is_not_a_model_call():
    line = json.dumps({"timestamp": "2026-10-05T08:00:00.000Z", "type": "event_msg",
                       "payload": {"type": "token_count", "info": None,
                                   "rate_limits": {"primary": None}}})
    assert cr.parse_line(line) is None


def test_an_mcp_err_result_is_an_error():
    line = json.dumps({"timestamp": "2026-10-05T08:00:00.000Z", "type": "event_msg",
                       "payload": {"type": "mcp_tool_call_end", "call_id": "c1",
                                   "result": {"Err": "user cancelled MCP tool call"}}})
    record = cr.parse_line(line)
    assert record.get("is_error") is True
    assert record.get("error") == "user cancelled MCP tool call"


def test_unreadable_lines_are_skipped(tmp_path):
    path = tmp_path / "rollout.jsonl"
    path.write_bytes(b'not json\n{"type":"event_msg"}\n\xff\xfe\n')
    assert cr.read_from(str(path), 0) == ([], path.stat().st_size)


def test_a_missing_file_reads_nothing(tmp_path):
    assert cr.read_from(str(tmp_path / "gone.jsonl"), 7) == ([], 7)
