"""The context warnings and the after-compaction line (rius_cc.context_warn).

A Claude Code user sees one short line when the context first passes 70% of
the window, another at 90%, and one after a compaction saying how much it
dropped. They are hook JSON `systemMessage`s computed from the tail of the
transcript; nothing about them reaches the model or the network.
"""
import contextlib
import io
import json
import os
import pathlib
import subprocess
import sys
import types

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "scripts"))

import hook as hook_mod  # noqa: E402
from rius_cc import context_warn, state  # noqa: E402
from tests.platforms import BASH  # noqa: E402
from tests.signed_in import sign_in  # noqa: E402
from tests.test_hook import (HOOK, _enabled_env, _isolated_env,  # noqa: E402
                             _run_in_process)

HOOK_SH = str(pathlib.Path(HOOK).with_name("hook.sh"))

SID = "11111111-1111-1111-1111-111111111111"
OPUS = "claude-opus-5-5"
T0 = 1790762400          # 2026-09-30T10:00:00Z

WARN_72 = ("Context is 72% full (144k of 200k). Quality may drop; consider "
           "/compact or a new session.")


class Transcript:
    """A synthetic transcript on disk, appended to row by row."""

    def __init__(self, path):
        self.path = path
        self.n = 0

    def _stamp(self, kind, **fields):
        self.n += 1
        row = {"type": kind, "uuid": "u%d" % self.n, "sessionId": SID,
               "timestamp": "2026-09-30T10:%02d:%02d.000Z"
                            % (self.n // 60, self.n % 60)}
        row.update(fields)
        return row

    def add(self, row):
        with open(self.path, "ab") as fh:
            fh.write(json.dumps(row).encode() + b"\n")

    def raw(self, data: bytes):
        with open(self.path, "ab") as fh:
            fh.write(data)

    def user(self, text="hello"):
        self.add(self._stamp("user", message={
            "role": "user", "content": [{"type": "text", "text": text}]}))

    def assistant(self, context, model=OPUS, requested=None, **row):
        usage = {"input_tokens": 6, "cache_creation_input_tokens": 1000,
                 "cache_read_input_tokens": context - 1006,
                 "output_tokens": 50}
        fields = {"message": {"role": "assistant", "model": model,
                              "content": [{"type": "text", "text": "ok"}],
                              "usage": usage}}
        if requested is not None:
            fields["requestedModel"] = requested
        fields.update(row)
        self.add(self._stamp("assistant", **fields))

    def boundary(self, pre=168000, post=31000, trigger="auto", **meta):
        data = {"trigger": trigger}
        if pre is not None:
            data["preTokens"] = pre
        if post is not None:
            data["postTokens"] = post
        data.update(meta)
        self.add(self._stamp("system", subtype="compact_boundary",
                             content="Conversation compacted",
                             compactMetadata=data))


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    (h / ".claude" / "rius").mkdir(parents=True)
    return str(h)


@pytest.fixture
def tr(tmp_path):
    return Transcript(str(tmp_path / "session.jsonl"))


def _cfg():
    return types.SimpleNamespace(debug=False, api_key=None)


def _fire(event, tr, home, source=None, env=None, sid=SID):
    payload = {"session_id": sid, "transcript_path": tr.path,
               "hook_event_name": event}
    if source:
        payload["source"] = source
    return context_warn.messages(event, payload, sid, _cfg(), home,
                                 env={} if env is None else env)


def stop(tr, home, **kw):
    return _fire("Stop", tr, home, **kw)


def compact_start(tr, home, **kw):
    return _fire("SessionStart", tr, home, source="compact", **kw)


def _record(home, sid=SID):
    path = os.path.join(home, ".claude", "rius", "notices", sid + ".context")
    with open(path) as fh:
        return json.load(fh)


# thresholds ---------------------------------------------------------------

def test_nothing_below_seventy_percent(tr, home):
    tr.assistant(139_000)
    assert stop(tr, home) == []


def test_seventy_percent_fires_once(tr, home):
    tr.assistant(144_000)
    assert stop(tr, home) == [WARN_72]
    assert stop(tr, home) == []
    tr.assistant(150_000)
    assert stop(tr, home) == []


def test_exactly_seventy_percent_fires(tr, home):
    tr.assistant(140_000)
    assert stop(tr, home) == [
        "Context is 70% full (140k of 200k). Quality may drop; consider "
        "/compact or a new session."]


def test_seventy_then_ninety_are_separate_messages(tr, home):
    tr.assistant(144_000)
    assert stop(tr, home) == [WARN_72]
    tr.assistant(182_400)
    assert stop(tr, home) == [
        "Context is 91% full (182k of 200k). Quality may drop; consider "
        "/compact or a new session."]
    tr.assistant(190_000)
    assert stop(tr, home) == []


def test_a_jump_past_ninety_fires_only_the_ninety_message(tr, home):
    tr.assistant(185_000)
    assert stop(tr, home) == [
        "Context is 92% full (185k of 200k). Quality may drop; consider "
        "/compact or a new session."]
    tr.assistant(150_000)       # both are marked shown: no 70% afterwards
    assert stop(tr, home) == []


def test_context_is_input_plus_both_cache_counts(tr, home):
    tr.add(tr._stamp("assistant", message={
        "role": "assistant", "model": OPUS, "content": [],
        "usage": {"input_tokens": 4000, "cache_creation_input_tokens": 40000,
                  "cache_read_input_tokens": 100000, "output_tokens": 9000}}))
    assert stop(tr, home) == [WARN_72]


def test_the_last_real_assistant_row_decides(tr, home):
    tr.assistant(190_000)
    tr.user()
    tr.assistant(60_000)
    assert stop(tr, home) == []


def test_only_stop_warns_about_the_threshold(tr, home):
    tr.assistant(190_000)
    for event in ("UserPromptSubmit", "PostToolUse", "PreToolUse",
                  "SessionEnd"):
        assert _fire(event, tr, home) == []
    assert _fire("SessionStart", tr, home, source="startup") == []
    assert _fire("SessionStart", tr, home, source="resume") == []
    assert _fire("SessionStart", tr, home, source="compact") == []


# which rows count ---------------------------------------------------------

def test_synthetic_and_api_error_rows_are_skipped(tr, home):
    tr.assistant(144_000)
    tr.assistant(0, model="<synthetic>", isApiErrorMessage=True)
    tr.assistant(1_006, model="<synthetic>")
    tr.assistant(190_000, model="<synthetic>")
    tr.assistant(190_000, isApiErrorMessage=True)
    assert stop(tr, home) == [WARN_72]


def test_rows_with_nothing_but_synthetic_ones_give_nothing(tr, home):
    tr.assistant(190_000, model="<synthetic>")
    tr.assistant(190_000, isApiErrorMessage=True)
    assert stop(tr, home) == []


def test_subagent_rows_are_skipped(tr, home):
    tr.assistant(60_000)
    tr.assistant(190_000, isSidechain=True)
    assert stop(tr, home) == []


def test_a_row_with_all_zero_usage_is_skipped(tr, home):
    tr.assistant(150_000)
    tr.add(tr._stamp("assistant", message={
        "role": "assistant", "model": OPUS, "content": [],
        "usage": {"input_tokens": 0, "cache_creation_input_tokens": 0,
                  "cache_read_input_tokens": 0, "output_tokens": 0}}))
    assert stop(tr, home) == ["Context is 75% full (150k of 200k). "
                              "Quality may drop; consider /compact or a "
                              "new session."]


# the window ---------------------------------------------------------------

def test_a_model_not_in_the_table_gets_no_warning(tr, home):
    tr.assistant(190_000, model="claude-sonnet-4-6")
    tr.assistant(190_000, model="some-other-model")
    assert stop(tr, home) == []
    assert not os.path.exists(os.path.join(home, ".claude", "rius", "notices"))


@pytest.mark.parametrize("model,known", [
    ("claude-opus-5-5", True), ("claude-opus-5", True),
    ("claude-sonnet-5-5", True), ("claude-sonnet-5", True),
    ("claude-fable-5-1", True), ("claude-opus-5-5-20261001", True),
    ("claude-opus-50", False), ("claude-sonnet-4-6", False),
    ("claude-haiku-5-5", False), ("claude-opus-4", False), (None, False)])
def test_windows_are_matched_by_model_id_prefix(model, known):
    assert (context_warn.windows_for(model) is not None) == known


def test_a_context_above_the_standard_window_proves_the_larger_one(tr, home):
    tr.assistant(250_000)
    assert stop(tr, home) == []                 # 25% of 1M
    tr.assistant(750_000)
    assert stop(tr, home) == [
        "Context is 75% full (750k of 1M). Quality may drop; consider "
        "/compact or a new session."]


def test_the_requested_model_marker_selects_the_larger_window(tr, home):
    tr.assistant(160_000, requested="claude-opus-5-5[1m]")
    assert stop(tr, home) == []


def test_a_requested_model_without_the_marker_is_the_standard_window(tr, home):
    tr.assistant(160_000, requested="claude-opus-5-5")
    assert stop(tr, home) == [
        "Context is 80% full (160k of 200k). Quality may drop; consider "
        "/compact or a new session."]


def test_once_the_larger_window_is_proven_it_stays_for_the_session(tr, home):
    tr.assistant(250_000)
    assert stop(tr, home) == []
    tr.assistant(150_000)                       # still a 1M session
    assert stop(tr, home) == []
    assert _record(home)["large"] is True


def test_a_compaction_from_above_the_standard_window_proves_it(tr, home):
    tr.assistant(250_000)
    tr.boundary(pre=250_000, post=30_000)
    tr.assistant(160_000)
    assert stop(tr, home) == ["Compacted 250k to 30k tokens. Check that "
                              "instructions you rely on are still followed."]


def test_window_for_treats_a_context_above_the_standard_window_as_proof():
    assert context_warn.window_for(OPUS, None, 250_000, False) == 1_000_000
    assert context_warn.window_for(OPUS, None, 200_000, False) == 200_000
    assert context_warn.window_for(OPUS, None, 10_000, True) == 1_000_000
    assert context_warn.window_for(OPUS, "x[1M]", 10_000, False) == 1_000_000
    assert context_warn.window_for("other", None, 10_000, True) is None


def test_number_formatting():
    assert [context_warn.tokens(n) for n in (
        144_000, 143_500, 143_499, 200_000, 1_000_000, 2_000_000, 1_050_000,
        31_000, 999, 0)] == ["144k", "144k", "143k", "200k", "1M", "2M",
                             "1050k", "31k", "999", "0"]


# opting out ---------------------------------------------------------------

@pytest.mark.parametrize("value", ["0", "false", "off", "FALSE", "Off", " 0 "])
def test_opt_out_spellings(tr, home, value):
    tr.assistant(190_000)
    tr.boundary()
    env = {"RIUS_CONTEXT_WARN": value}
    assert stop(tr, home, env=env) == []
    assert compact_start(tr, home, env=env) == []
    assert not os.path.exists(os.path.join(home, ".claude", "rius", "notices"))


@pytest.mark.parametrize("env", [{}, {"RIUS_CONTEXT_WARN": ""},
                                 {"RIUS_CONTEXT_WARN": "1"},
                                 {"RIUS_CONTEXT_WARN": "true"},
                                 {"RIUS_CONTEXT_WARN": "on"}])
def test_on_by_default_and_for_other_values(tr, home, env):
    tr.assistant(144_000)
    assert stop(tr, home, env=env) == [WARN_72]


def test_messages_reads_the_process_environment_by_default(
        tr, home, monkeypatch):
    tr.assistant(144_000)
    monkeypatch.setenv("RIUS_CONTEXT_WARN", "off")
    payload = {"transcript_path": tr.path}
    assert context_warn.messages("Stop", payload, SID, _cfg(), home) == []
    monkeypatch.delenv("RIUS_CONTEXT_WARN")
    assert context_warn.messages("Stop", payload, SID, _cfg(), home) == [WARN_72]


# the compaction line -------------------------------------------------------

LINE = ("Compacted 168k to 31k tokens. Check that instructions you rely on "
        "are still followed.")


def test_session_start_after_compact_says_how_much_was_dropped(tr, home):
    tr.assistant(167_000)
    tr.boundary(pre=167_600, post=31_400)
    assert compact_start(tr, home) == [LINE]


def test_the_latest_boundary_is_the_one_reported(tr, home):
    tr.boundary(pre=90_000, post=10_000)
    tr.assistant(120_000)
    tr.boundary(pre=167_600, post=31_400)
    assert compact_start(tr, home) == [LINE]


def test_a_boundary_is_reported_once(tr, home):
    tr.boundary(pre=167_600, post=31_400)
    assert compact_start(tr, home) == [LINE]
    assert compact_start(tr, home) == []
    assert stop(tr, home) == []


def test_a_newer_boundary_is_reported_again(tr, home):
    tr.boundary(pre=167_600, post=31_400)
    assert compact_start(tr, home) == [LINE]
    tr.assistant(60_000)
    tr.boundary(pre=170_000, post=25_000)
    assert stop(tr, home) == ["Compacted 170k to 25k tokens. Check that "
                              "instructions you rely on are still followed."]


def test_stop_reports_a_boundary_session_start_did_not_show(tr, home):
    tr.assistant(100_000)
    tr.boundary(pre=167_600, post=31_400)
    assert stop(tr, home) == [LINE]
    assert stop(tr, home) == []
    assert compact_start(tr, home) == []


@pytest.mark.parametrize("post", [None, 0])
def test_without_post_tokens_only_the_before_size_is_given(tr, home, post):
    tr.boundary(pre=167_600, post=post)
    assert compact_start(tr, home) == ["Compacted from 168k tokens."]


@pytest.mark.parametrize("pre", [None, 0])
def test_without_pre_tokens_nothing_is_said(tr, home, pre):
    tr.boundary(pre=pre, post=31_000)
    assert compact_start(tr, home) == []
    assert stop(tr, home) == []


def test_a_boundary_with_unusable_sizes_says_nothing(tr, home):
    tr.boundary(pre="168000", post=[1])
    tr.boundary(pre=True, post=-5)
    assert compact_start(tr, home) == []


def test_no_boundary_at_session_start_says_nothing(tr, home):
    tr.assistant(100_000)
    assert compact_start(tr, home) == []


def test_an_unreadable_transcript_says_nothing(tmp_path, home):
    missing = Transcript(str(tmp_path / "nope.jsonl"))
    assert compact_start(missing, home) == []
    assert stop(missing, home) == []
    as_dir = Transcript(str(tmp_path))
    assert compact_start(as_dir, home) == []


def test_a_bad_transcript_path_in_the_payload_says_nothing(home):
    for path in (None, "", 7, ["x"]):
        payload = {"transcript_path": path, "source": "compact"}
        assert context_warn.messages("SessionStart", payload, SID, _cfg(),
                                     home, env={}) == []
    assert context_warn.messages("Stop", {}, SID, _cfg(), home, env={}) == []


def test_session_start_other_than_compact_does_not_report_a_boundary(tr, home):
    tr.boundary()
    for source in ("startup", "resume", "clear", None):
        assert _fire("SessionStart", tr, home, source=source) == []


def test_a_boundary_with_no_uuid_is_deduplicated_by_its_timestamp(tr, home):
    row = tr._stamp("system", subtype="compact_boundary", uuid="",
                    compactMetadata={"trigger": "auto", "preTokens": 167600,
                                     "postTokens": 31400})
    tr.add(row)
    assert compact_start(tr, home) == [LINE]
    assert stop(tr, home) == []


# compaction re-arms the thresholds ------------------------------------------

def test_the_thresholds_re_arm_after_a_compaction(tr, home):
    tr.assistant(185_000)
    assert len(stop(tr, home)) == 1
    tr.boundary(pre=185_000, post=30_000)
    tr.assistant(40_000)
    assert stop(tr, home) == [
        "Compacted 185k to 30k tokens. Check that instructions you rely on "
        "are still followed."]
    tr.assistant(144_000)
    assert stop(tr, home) == [WARN_72]
    tr.assistant(182_400)
    assert len(stop(tr, home)) == 1
    assert stop(tr, home) == []


def test_a_row_from_before_the_compaction_is_not_the_context(tr, home):
    tr.assistant(190_000)
    tr.boundary(pre=190_000, post=30_000)
    out = stop(tr, home)
    assert out == ["Compacted 190k to 30k tokens. Check that instructions "
                   "you rely on are still followed."]
    assert _record(home)["level"] == 0


def test_a_stale_row_does_not_warn_when_the_boundary_was_already_shown(
        tr, home):
    tr.assistant(190_000)
    tr.boundary(pre=190_000, post=30_000)
    assert compact_start(tr, home) == [
        "Compacted 190k to 30k tokens. Check that instructions you rely on "
        "are still followed."]
    assert stop(tr, home) == []


# reading the tail ------------------------------------------------------------

class _Reads:
    """Counts the bytes context_warn reads from the files it opens."""

    def __init__(self, monkeypatch):
        self.bytes = 0
        self.largest = 0
        real_open = open
        reads = self

        class Counting:
            def __init__(self, fh):
                self.fh = fh

            def read(self, *args):
                data = self.fh.read(*args)
                reads.bytes += len(data)
                reads.largest = max(reads.largest, len(data))
                return data

            def __getattr__(self, name):
                return getattr(self.fh, name)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self.fh.close()

        monkeypatch.setattr(context_warn, "open",
                            lambda *a, **k: Counting(real_open(*a, **k)),
                            raising=False)


def _filler(tr, megabytes):
    """Rows of tool output that make the file much bigger than the scan cap."""
    row = json.dumps(tr._stamp("user", message={
        "role": "user", "content": [{"type": "text", "text": "x" * 4000}]}))
    with open(tr.path, "ab") as fh:
        for _ in range(megabytes * 1024 * 1024 // len(row)):
            fh.write(row.encode() + b"\n")


def test_a_huge_transcript_is_read_from_the_tail_only(tr, home, monkeypatch):
    tr.assistant(190_000)            # at the start: the 90% row must not be seen
    _filler(tr, 6)
    tr.assistant(144_000)
    size = os.path.getsize(tr.path)
    assert size > 6 * 1024 * 1024
    reads = _Reads(monkeypatch)
    assert stop(tr, home) == [WARN_72]
    assert reads.bytes <= context_warn.MAX_SCAN_BYTES + 4096
    assert reads.bytes < size / 2
    assert reads.largest <= context_warn.CHUNK_BYTES


def test_a_compaction_beyond_the_scan_cap_is_not_looked_for(tr, home):
    tr.boundary()
    _filler(tr, 3)
    tr.assistant(100_000)
    assert stop(tr, home) == []


def test_a_row_before_a_line_longer_than_a_chunk_is_found(tr, home):
    tr.assistant(144_000)
    tr.user("y" * (3 * context_warn.CHUNK_BYTES))
    tr.user("z")
    assert stop(tr, home) == [WARN_72]


def test_a_boundary_just_inside_the_scan_cap_is_found(tr, home):
    tr.boundary(pre=167_600, post=31_400)
    _filler(tr, 1)
    assert stop(tr, home) == [LINE]


def test_a_corrupt_last_line_is_skipped(tr, home):
    tr.assistant(144_000)
    tr.raw(b'{"type":"assistant","uuid":"x","message":{"usage":{"input_tok')
    assert stop(tr, home) == [WARN_72]


def test_garbage_lines_are_skipped(tr, home):
    tr.assistant(144_000)
    tr.raw(b"\xff\xfe not utf-8 \"usage\" \"assistant\"\n")
    tr.raw(b'[1, 2, "usage", "assistant"]\n')
    tr.raw(b'{"type":"assistant","message":{"model":"%s","usage":'
           b'"usage"}}\n' % OPUS.encode())
    tr.raw(b'{"type":"assistant","uuid":"u","timestamp":"bad","message":{}}\n')
    tr.raw(b'"compact_boundary" is not json\n')
    assert stop(tr, home) == [WARN_72]


def test_an_empty_transcript_says_nothing(tr, home):
    open(tr.path, "wb").close()
    assert stop(tr, home) == []
    assert compact_start(tr, home) == []


def test_a_transcript_with_no_trailing_newline_is_read(tr, home):
    tr.assistant(144_000)
    with open(tr.path, "rb+") as fh:
        data = fh.read().rstrip(b"\n")
        fh.seek(0)
        fh.truncate()
        fh.write(data)
    assert stop(tr, home) == [WARN_72]


# what is kept ------------------------------------------------------------------

def test_the_record_holds_sizes_and_ids_only(tr, home):
    tr.user("SECRET-PROMPT-TEXT-1234")
    tr.assistant(144_000)
    tr.boundary()
    tr.assistant(144_000)
    stop(tr, home)
    path = os.path.join(home, ".claude", "rius", "notices", SID + ".context")
    text = open(path).read()
    assert set(json.loads(text)) == {"level", "boundary", "large"}
    assert "SECRET" not in text


def test_a_corrupt_record_counts_as_nothing_shown(tr, home):
    tr.assistant(144_000)
    notices = os.path.join(home, ".claude", "rius", "notices")
    os.makedirs(notices)
    with open(os.path.join(notices, SID + ".context"), "w") as fh:
        fh.write("{not json")
    assert stop(tr, home) == [WARN_72]
    assert stop(tr, home) == []


def test_a_conversation_moved_to_a_new_id_keeps_what_it_was_shown(tr, home):
    old = "22222222-2222-2222-2222-222222222222"
    tr.assistant(144_000)
    assert stop(tr, home, sid=old) == [WARN_72]
    st = state.new_state()
    st["continued_from"] = old
    state.save(SID, home, st)
    assert stop(tr, home) == []


def test_a_taken_over_record_is_kept_under_the_new_id(tr, home):
    old = "22222222-2222-2222-2222-222222222222"
    tr.assistant(144_000)
    assert stop(tr, home, sid=old) == [WARN_72]
    st = state.new_state()
    st["continued_from"] = old
    state.save(SID, home, st)
    assert stop(tr, home) == []
    assert _record(home)["level"] == 70
    os.remove(os.path.join(home, ".claude", "rius", "notices",
                           old + ".context"))
    assert stop(tr, home) == []


def test_a_context_that_fell_warns_again_without_a_boundary_in_sight(tr, home):
    tr.assistant(144_000)
    assert stop(tr, home) == [WARN_72]
    tr.boundary()
    _filler(tr, 3)                 # the compaction is beyond the scan cap
    tr.assistant(30_000)
    assert stop(tr, home) == []
    assert _record(home)["level"] == 0
    tr.assistant(144_000)
    assert stop(tr, home) == [WARN_72]


def test_two_sessions_are_kept_apart(tr, home):
    other = "33333333-3333-3333-3333-333333333333"
    tr.assistant(144_000)
    assert stop(tr, home) == [WARN_72]
    assert stop(tr, home, sid=other) == [WARN_72]


def test_an_unwritable_record_never_raises(tr, home):
    tr.assistant(144_000)
    notices = os.path.join(home, ".claude", "rius", "notices")
    with open(notices, "w") as fh:          # a file where the directory goes
        fh.write("")
    assert stop(tr, home) == []


# the hook ------------------------------------------------------------------------

def _hook(monkeypatch, capsys, home, event, payload, env=None, argv=()):
    environ = {"HOME": home, "USERPROFILE": home}
    environ.update(env or {})
    monkeypatch.setattr(hook_mod.subprocess, "Popen", _no_spawn)
    monkeypatch.setattr(os, "environ", environ)
    monkeypatch.setattr(sys, "argv", ["hook.py"] + list(argv) + [event])
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    capsys.readouterr()
    hook_mod.main()
    return capsys.readouterr().out


def _no_spawn(*args, **kwargs):
    raise OSError("no children in this test")


def _only_message(stdout):
    """The output is exactly one hook JSON object that carries nothing for
    the model, and its message."""
    assert stdout.endswith("\n") and stdout.count("\n") == 1, stdout
    out = json.loads(stdout)
    assert set(out) == {"systemMessage"}
    return out["systemMessage"]


def _payload(tr, tmp_path, event, **extra):
    payload = {"session_id": SID, "cwd": str(tmp_path),
               "transcript_path": tr.path, "hook_event_name": event}
    payload.update(extra)
    return payload


def test_stop_prints_one_hook_json_object(tr, home, tmp_path, monkeypatch,
                                          capsys):
    tr.assistant(144_000)
    out = _hook(monkeypatch, capsys, home, "Stop", _payload(tr, tmp_path, "Stop"))
    assert _only_message(out) == WARN_72
    again = _hook(monkeypatch, capsys, home, "Stop",
                  _payload(tr, tmp_path, "Stop"))
    assert again == ""


def test_the_session_start_notice_and_the_compaction_line_are_one_object(
        tr, home, tmp_path, monkeypatch, capsys):
    from rius_cc import config
    from tests import signed_in
    signed_in.sign_in(home, api_key="gf_key.sig", workspace_id="w1",
                      workspace_name="acme")
    config.write_path_rules(home, {"enabled_paths": [str(tmp_path)]})
    tr.boundary(pre=167_600, post=31_400)
    out = _hook(monkeypatch, capsys, home, "SessionStart",
                _payload(tr, tmp_path, "SessionStart", source="compact"))
    assert _only_message(out) == (
        "Rius: tracing this session to workspace acme (content: on)\n" + LINE)


def test_the_compaction_line_comes_without_a_notice_once_the_notice_is_shown(
        tr, home, tmp_path, monkeypatch, capsys):
    from rius_cc import config
    from tests import signed_in
    signed_in.sign_in(home, api_key="gf_key.sig", workspace_id="w1",
                      workspace_name="acme")
    config.write_path_rules(home, {"enabled_paths": [str(tmp_path)]})
    _hook(monkeypatch, capsys, home, "SessionStart",
          _payload(tr, tmp_path, "SessionStart", source="startup"))
    tr.boundary(pre=167_600, post=31_400)
    out = _hook(monkeypatch, capsys, home, "SessionStart",
                _payload(tr, tmp_path, "SessionStart", source="compact"))
    assert _only_message(out) == LINE


def test_the_warning_needs_neither_a_sign_in_nor_tracing(
        tr, home, tmp_path, monkeypatch, capsys):
    tr.assistant(144_000)       # no key, folder not enabled
    out = _hook(monkeypatch, capsys, home, "Stop", _payload(tr, tmp_path, "Stop"))
    assert _only_message(out) == WARN_72


def test_the_hook_honours_the_opt_out(tr, home, tmp_path, monkeypatch, capsys):
    tr.assistant(190_000)
    tr.boundary()
    env = {"RIUS_CONTEXT_WARN": "false"}
    assert _hook(monkeypatch, capsys, home, "Stop",
                 _payload(tr, tmp_path, "Stop"), env) == ""
    assert _hook(monkeypatch, capsys, home, "SessionStart",
                 _payload(tr, tmp_path, "SessionStart", source="compact"),
                 env).count("Compacted") == 0


def test_other_events_print_nothing(tr, home, tmp_path, monkeypatch, capsys):
    tr.assistant(190_000)
    for event in ("UserPromptSubmit", "PreToolUse", "PostToolUse",
                  "SessionEnd"):
        assert _hook(monkeypatch, capsys, home, event,
                     _payload(tr, tmp_path, event)) == ""


def test_a_failing_check_does_not_break_the_hook(tr, home, tmp_path,
                                                 monkeypatch, capsys):
    tr.assistant(144_000)

    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(context_warn, "read_tail", boom)
    assert _hook(monkeypatch, capsys, home, "Stop",
                 _payload(tr, tmp_path, "Stop")) == ""


def _exporter_spawns(tr, tmp_path, monkeypatch):
    env, home = _enabled_env(tmp_path)
    calls = _run_in_process(monkeypatch, "Stop", _payload(tr, tmp_path, "Stop"),
                            env, home)
    return [c for c in calls if any(str(a).endswith("exporter.py")
                                    for a in c["argv"])]


def test_tracing_still_runs_when_the_check_fails(tr, tmp_path, monkeypatch):
    tr.assistant(144_000)

    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(context_warn, "read_tail", boom)
    assert len(_exporter_spawns(tr, tmp_path, monkeypatch)) == 1


def test_tracing_still_runs_when_the_module_cannot_be_imported(
        tr, tmp_path, monkeypatch):
    import rius_cc
    tr.assistant(144_000)
    monkeypatch.setitem(sys.modules, "rius_cc.context_warn", None)
    monkeypatch.delattr(rius_cc, "context_warn")
    assert len(_exporter_spawns(tr, tmp_path, monkeypatch)) == 1


def test_tracing_still_runs_alongside_a_warning(tr, tmp_path, monkeypatch,
                                                capsys):
    tr.assistant(144_000)
    assert len(_exporter_spawns(tr, tmp_path, monkeypatch)) == 1
    assert _only_message(capsys.readouterr().out) == WARN_72


# end to end: the real script ------------------------------------------------------

def _run_hook(event, payload, env=None, agent_flag=()):
    return subprocess.run(
        [sys.executable, HOOK] + list(agent_flag) + [event],
        input=json.dumps(payload),
        capture_output=True, text=True, env=env or _isolated_env(), timeout=30)


def test_e2e_stop_prints_exactly_one_system_message(tr, tmp_path):
    tr.user("hi")
    tr.assistant(144_000)
    r = _run_hook("Stop", _payload(tr, tmp_path, "Stop"))
    assert r.returncode == 0
    assert _only_message(r.stdout) == WARN_72
    assert r.stderr == ""


def test_e2e_below_the_threshold_prints_nothing(tr, tmp_path):
    tr.assistant(100_000)
    r = _run_hook("Stop", _payload(tr, tmp_path, "Stop"))
    assert (r.returncode, r.stdout) == (0, "")


def test_e2e_opt_out_prints_nothing(tr, tmp_path):
    tr.assistant(190_000)
    env = _isolated_env()
    env["RIUS_CONTEXT_WARN"] = "off"
    r = _run_hook("Stop", _payload(tr, tmp_path, "Stop"), env)
    assert (r.returncode, r.stdout) == (0, "")


def test_e2e_the_warning_is_once_per_threshold_across_processes(tr, tmp_path):
    env = _isolated_env()
    tr.assistant(144_000)
    first = _run_hook("Stop", _payload(tr, tmp_path, "Stop"), env)
    second = _run_hook("Stop", _payload(tr, tmp_path, "Stop"), env)
    tr.assistant(185_000)
    third = _run_hook("Stop", _payload(tr, tmp_path, "Stop"), env)
    assert _only_message(first.stdout) == WARN_72
    assert second.stdout == ""
    assert _only_message(third.stdout).startswith("Context is 92% full")


def test_e2e_compact_session_start_prints_the_compaction_line(tr, tmp_path):
    tr.assistant(167_000)
    tr.boundary(pre=167_600, post=31_400)
    r = _run_hook("SessionStart", _payload(tr, tmp_path, "SessionStart",
                                           source="compact"))
    assert r.returncode == 0
    message = _only_message(r.stdout)
    assert message.endswith(LINE)


def _notices(env):
    return [os.path.join(env["HOME"], d, "rius", "notices")
            for d in (".claude", ".codex", ".cursor")]


def test_e2e_codex_hooks_print_nothing_and_keep_no_record(tr, tmp_path):
    tr.assistant(190_000)
    env = _isolated_env()
    r = _run_hook("Stop", _payload(tr, tmp_path, "Stop"), env,
                  ["--agent", "codex"])
    assert (r.returncode, r.stdout) == (0, "")
    assert not any(os.path.exists(path) for path in _notices(env))


def test_e2e_cursor_hooks_print_no_warning_and_keep_no_record(tr, tmp_path):
    tr.assistant(190_000)
    env = _isolated_env()
    payload = _payload(tr, tmp_path, "stop", conversation_id="c1",
                       cursor_version="1.0")
    r = _run_hook("stop", payload, env, ["--agent", "cursor"])
    assert r.returncode == 0
    assert "Context is" not in r.stdout
    assert not any(os.path.exists(path) for path in _notices(env))


def test_e2e_a_cursor_payload_on_the_claude_path_prints_nothing(tr, tmp_path):
    tr.assistant(190_000)
    env = _isolated_env()
    payload = _payload(tr, tmp_path, "Stop", conversation_id="c1")
    r = _run_hook("Stop", payload, env)
    assert (r.returncode, r.stdout) == (0, "")
    assert not any(os.path.exists(path) for path in _notices(env))


def _run_launcher(env, payload):
    return subprocess.run([BASH, HOOK_SH, "Stop"], input=json.dumps(payload),
                          capture_output=True, text=True, env=env, timeout=30)


def test_e2e_through_the_launcher_a_signed_in_user_is_warned_with_tracing_off(
        tr, tmp_path):
    tr.assistant(144_000)
    env = _isolated_env()
    sign_in(env["HOME"])                    # a key, but no folder is enabled
    r = _run_launcher(env, _payload(tr, tmp_path, "Stop"))
    assert r.returncode == 0
    assert _only_message(r.stdout) == WARN_72


def test_e2e_through_the_launcher_a_signed_out_user_pays_for_nothing(
        tr, tmp_path):
    """hook.sh exits before Python on a machine with no key (RIUS-1224), so
    there is no warning there either: the cost of the check is not paid by
    anyone who gets nothing else from the plugin."""
    tr.assistant(144_000)
    r = _run_launcher(_isolated_env(), _payload(tr, tmp_path, "Stop"))
    assert (r.returncode, r.stdout) == (0, "")
