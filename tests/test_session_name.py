"""The trace is named after the Claude Code session.

The console titles a trace with its root span's NAME (the trace list's Name
column, the detail header), so the root carries the session's own name
instead of a fixed "claude-code session". Claude Code writes that name into
the transcript: a `custom-title` record for /rename, --name or a hook's
sessionTitle, and an `ai-title` record summarising the first prompt. A
/rename fires no hook, so the next hook's read of the transcript is where
the plugin learns of it.

Both kinds are content: an ai-title is a summary of the prompt, and a bare
/rename generates its title from the conversation. With capture off the
root keeps the default name and no title reaches the state file.
"""
import json
import pathlib

import pytest

import exporter
from rius_cc import config, spans, state, transcript

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
SID = "33333333-3333-3333-3333-333333333333"
ENV = {"RIUS_API_KEY": "glassflow_k", "RIUS_ENDPOINT": "https://ingest.test"}
ROOT = spans.span_id_for("session:" + SID)
DEFAULT = "claude-code session"


def custom_title(title):
    return {"type": "custom-title", "customTitle": title, "sessionId": SID}


def ai_title(title):
    return {"type": "ai-title", "aiTitle": title, "sessionId": SID}


def prompt(uuid, prompt_id, ts, text="carry on"):
    return {"parentUuid": None, "isSidechain": False, "type": "user",
            "uuid": uuid, "timestamp": ts, "sessionId": SID,
            "promptId": prompt_id, "cwd": "/tmp/proj", "gitBranch": "main",
            "version": "2.1.284",
            "message": {"role": "user", "content": [
                {"type": "text", "text": text}]}}


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    (h / ".claude" / "rius").mkdir(parents=True)
    _rules(str(h), enabled=["/tmp"])
    return str(h)


@pytest.fixture
def sent(monkeypatch):
    batches = []
    monkeypatch.setattr(exporter.otlp, "encode",
                        lambda resource, out: batches.append(list(out)) or b"x")
    monkeypatch.setattr(exporter.otlp, "export",
                        lambda ep, key, body, timeout=5.0: 200)
    return batches


def _rules(home, enabled=(), disabled=()):
    with open(config.path_rules_path(home), "w") as fh:
        json.dump({"enabled_paths": list(enabled),
                   "disabled_paths": list(disabled)}, fh)


def _transcript(tmp_path, *records):
    path = tmp_path / (SID + ".jsonl")
    path.write_text("")
    _append(path, *records)
    return path


def _append(path, *records):
    with open(path, "a") as fh:
        for record in records:
            fh.write(json.dumps(record) + "\n")


def _run(event, path, home, env=ENV):
    payload = {"session_id": SID, "transcript_path": str(path),
               "cwd": "/tmp/proj", "hook_event_name": event}
    return exporter.run(event, payload, env, home)


def _roots(batches):
    return [s for batch in batches for s in batch if s.span_id == ROOT]


def _last_root(batches):
    return _roots(batches)[-1]


# --- reading the title records ----------------------------------------------

def test_read_from_reports_the_latest_title_of_each_kind(tmp_path):
    path = _transcript(tmp_path, custom_title("first"),
                       prompt("u1", "p1", "2026-09-30T10:00:00.000Z"),
                       ai_title("Summary of the prompt"),
                       custom_title("second"))
    titles = {}
    entries, _ = transcript.read_from(str(path), 0, titles=titles)
    assert [e.uuid for e in entries] == ["u1"]
    assert titles == {"custom": "second", "ai": "Summary of the prompt"}


def test_read_from_leaves_titles_empty_without_title_records(tmp_path):
    path = _transcript(tmp_path, prompt("u1", "p1", "2026-09-30T10:00:00.000Z"))
    titles = {}
    transcript.read_from(str(path), 0, titles=titles)
    assert titles == {}


# --- naming the root ---------------------------------------------------------

def test_the_root_is_named_after_the_custom_title(home, sent, tmp_path):
    path = _transcript(tmp_path, custom_title("rius-1070-orphaned-parents"),
                       prompt("u1", "p1", "2026-09-30T10:00:00.000Z"))
    _run("UserPromptSubmit", path, home)
    root = _last_root(sent)
    assert root.pending and root.name == "rius-1070-orphaned-parents"

    _run("SessionEnd", path, home)
    root = _last_root(sent)
    assert not root.pending and root.name == "rius-1070-orphaned-parents"


def test_the_ai_title_names_a_session_nobody_renamed(home, sent, tmp_path):
    path = _transcript(tmp_path, prompt("u1", "p1", "2026-09-30T10:00:00.000Z"),
                       ai_title("RIUS-1070 orphaned parents"))
    _run("SessionEnd", path, home)
    assert _last_root(sent).name == "RIUS-1070 orphaned parents"


def test_a_custom_title_beats_an_ai_title_written_after_it(home, sent, tmp_path):
    path = _transcript(tmp_path, custom_title("mine"),
                       prompt("u1", "p1", "2026-09-30T10:00:00.000Z"),
                       ai_title("Generated"))
    _run("SessionEnd", path, home)
    assert _last_root(sent).name == "mine"


def test_without_any_title_the_root_keeps_the_default_name(home, sent, tmp_path):
    path = _transcript(tmp_path, prompt("u1", "p1", "2026-09-30T10:00:00.000Z"))
    _run("SessionEnd", path, home)
    assert [r.name for r in _roots(sent)] == [DEFAULT, DEFAULT]


def test_an_emptied_custom_title_falls_back_to_the_ai_title(home, sent, tmp_path):
    path = _transcript(tmp_path, custom_title("mine"),
                       prompt("u1", "p1", "2026-09-30T10:00:00.000Z"),
                       ai_title("Generated"), custom_title(""))
    _run("SessionEnd", path, home)
    assert _last_root(sent).name == "Generated"


# --- a rename mid-session ----------------------------------------------------

def test_a_rename_mid_session_renames_the_trace(home, sent, tmp_path):
    path = _transcript(tmp_path, custom_title("before"),
                       prompt("u1", "p1", "2026-09-30T10:00:00.000Z"))
    _run("Stop", path, home)
    first = _last_root(sent)

    # What /rename appends: the record, then the command's own entries.
    _append(path, custom_title("after"),
            prompt("u2", "p2", "2026-09-30T10:05:00.000Z",
                   "<command-name>/rename</command-name>"))
    _run("Stop", path, home)
    renamed = _last_root(sent)
    assert renamed.pending and renamed.name == "after"
    # The same row in the backend: ReplacingMergeTree replaces it only when
    # the span id and start (part of the sort key) are unchanged.
    assert (renamed.span_id, renamed.start_ns) == (first.span_id, first.start_ns)

    _run("SessionEnd", path, home)
    assert _last_root(sent).name == "after"
    assert not _last_root(sent).pending


def test_every_copy_of_the_root_shares_one_start(home, sent, tmp_path):
    # The start is in the backend's sort key: a copy that differs by one
    # nanosecond is a second root row, not a replacement.
    path = _transcript(tmp_path, custom_title("before"),
                       prompt("u1", "p1", "2026-09-30T10:00:00.000Z"))
    _run("Stop", path, home)
    _append(path, custom_title("after"),
            prompt("u2", "p2", "2026-09-30T10:05:00.000Z"))
    _run("Stop", path, home)
    _run("SessionEnd", path, home)
    roots = _roots(sent)
    assert [r.pending for r in roots] == [True, True, False]
    assert len({r.start_ns for r in roots}) == 1


def test_an_unchanged_name_does_not_resend_the_root(home, sent, tmp_path):
    path = _transcript(tmp_path, custom_title("same"),
                       prompt("u1", "p1", "2026-09-30T10:00:00.000Z"))
    _run("Stop", path, home)
    _append(path, custom_title("same"),
            prompt("u2", "p2", "2026-09-30T10:05:00.000Z"))
    _run("Stop", path, home)
    assert len(_roots(sent)) == 1


def test_a_rename_just_before_quitting_reaches_the_closing_root(home, sent,
                                                                tmp_path):
    path = _transcript(tmp_path, prompt("u1", "p1", "2026-09-30T10:00:00.000Z"))
    _run("Stop", path, home)
    _append(path, custom_title("last-minute"))
    _run("SessionEnd", path, home)
    closing = [r for r in _roots(sent) if not r.pending]
    assert [r.name for r in closing] == ["last-minute"]


def test_a_title_is_one_line_and_capped(home, sent, tmp_path):
    path = _transcript(tmp_path, custom_title("  two\nlines\tand\x07more " + "x" * 300),
                       prompt("u1", "p1", "2026-09-30T10:00:00.000Z"))
    _run("SessionEnd", path, home)
    name = _last_root(sent).name
    assert name.startswith("two lines and more xxx")
    assert len(name) == spans.TITLE_MAX_CHARS


# --- capture off ---------------------------------------------------------------

NO_CAPTURE = dict(ENV, RIUS_CAPTURE_CONTENT="false")


def test_capture_off_sends_no_title_and_stores_none(home, sent, tmp_path):
    path = _transcript(tmp_path, custom_title("secret-customer-name"),
                       prompt("u1", "p1", "2026-09-30T10:00:00.000Z"),
                       ai_title("Summary of a secret prompt"))
    _run("Stop", path, home, env=NO_CAPTURE)
    _append(path, custom_title("renamed-secret"))
    _run("SessionEnd", path, home, env=NO_CAPTURE)

    assert {r.name for r in _roots(sent)} == {DEFAULT}
    on_disk = pathlib.Path(state.state_path(SID, home)).read_text()
    for title in ("secret-customer-name", "Summary of a secret prompt",
                  "renamed-secret"):
        assert title not in on_disk


def test_turning_capture_off_drops_a_stored_title(home, sent, tmp_path):
    path = _transcript(tmp_path, custom_title("sent-while-on"),
                       prompt("u1", "p1", "2026-09-30T10:00:00.000Z"))
    _run("Stop", path, home)
    assert _last_root(sent).name == "sent-while-on"

    _append(path, prompt("u2", "p2", "2026-09-30T10:05:00.000Z"))
    _run("SessionEnd", path, home, env=NO_CAPTURE)
    assert _last_root(sent).name == DEFAULT
    assert "sent-while-on" not in pathlib.Path(
        state.state_path(SID, home)).read_text()


def test_re_closing_a_finished_trace_with_capture_off_sends_no_title(
        home, sent, tmp_path):
    # A resumed session that ends before writing a new entry re-closes the
    # root without build() seeing anything new to emit.
    path = _transcript(tmp_path, custom_title("sent-while-on"),
                       prompt("u1", "p1", "2026-09-30T10:00:00.000Z"))
    _run("SessionEnd", path, home)
    assert _last_root(sent).name == "sent-while-on"

    # A finished trace gets no pending copy: it would never replace the
    # finished row, only sit behind it.
    before = len(_roots(sent))
    _run("Stop", path, home, env=NO_CAPTURE)
    assert len(_roots(sent)) == before

    _run("SessionEnd", path, home, env=NO_CAPTURE)
    assert [r.pending for r in _roots(sent)[before:]] == [False]
    assert _last_root(sent).name == DEFAULT
    assert "sent-while-on" not in pathlib.Path(
        state.state_path(SID, home)).read_text()


# --- a session disabled mid-way --------------------------------------------------

def _disable(home):
    _rules(home, enabled=["/tmp"], disabled=["/tmp/proj"])


def test_a_stopped_session_closes_under_the_name_it_already_sent(home, sent,
                                                                  tmp_path):
    path = _transcript(tmp_path, custom_title("sent-before-the-disable"),
                       prompt("u1", "p1", "2026-09-30T10:00:00.000Z"))
    _run("Stop", path, home)
    _disable(home)
    _run("SessionEnd", path, home)
    closing = _last_root(sent)
    assert not closing.pending and closing.name == "sent-before-the-disable"


def test_a_rename_after_the_disable_never_leaves_the_machine(home, sent,
                                                             tmp_path):
    path = _transcript(tmp_path, custom_title("sent-before-the-disable"),
                       prompt("u1", "p1", "2026-09-30T10:00:00.000Z"))
    _run("Stop", path, home)
    _disable(home)
    _append(path, custom_title("typed-after-the-disable"),
            prompt("u2", "p2", "2026-09-30T10:05:00.000Z"))
    for event in ("Stop", "SessionEnd"):
        _run(event, path, home)
    names = [s.name for batch in sent for s in batch]
    assert "typed-after-the-disable" not in names
    assert _last_root(sent).name == "sent-before-the-disable"
