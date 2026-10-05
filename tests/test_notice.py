"""The one line Rius shows the user at SessionStart (rius_cc.notice)."""
import contextlib
import io
import json
import os
import sys
import time

import pytest

import exporter
import hook as hook_mod
import rius_ctl
from rius_cc import config, login, notice, state

SID = "11111111-1111-1111-1111-111111111111"


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    (h / ".claude" / "rius").mkdir(parents=True)
    return str(h)


def _enable(home, folder):
    config.write_path_rules(home, {"enabled_paths": [folder]})


def _sign_in(home, workspace="acme"):
    login._write_private(login.credentials_path(home), {
        "api_key": "gf_key.sig", "endpoint": "https://ingest.test",
        "workspace_id": "w1", "workspace_name": workspace,
        "email": "a@b.c"})


class _Session:
    """Runs hook.main() for SessionStart in-process, with its detached
    children faked away, and returns what it printed."""

    def __init__(self, monkeypatch, capsys):
        self.monkeypatch = monkeypatch
        self.capsys = capsys
        monkeypatch.setattr(hook_mod.subprocess, "Popen", _no_spawn)

    def __call__(self, home, cwd, sid=SID, env=None, source="startup"):
        environ = {"HOME": home, "USERPROFILE": home}
        environ.update(env or {})
        self.monkeypatch.setattr(os, "environ", environ)
        self.monkeypatch.setattr(sys, "argv", ["hook.py", "SessionStart"])
        self.monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(
            {"session_id": sid, "cwd": cwd, "source": source,
             "transcript_path": "/nonexistent.jsonl"})))
        self.capsys.readouterr()
        hook_mod.main()
        return self.capsys.readouterr().out


def _no_spawn(*args, **kwargs):
    raise OSError("no children in this test")


@pytest.fixture
def session_start(monkeypatch, capsys):
    return _Session(monkeypatch, capsys)


def _message(stdout):
    """The systemMessage of the hook's output, which must be one valid hook
    JSON object carrying nothing for the model."""
    assert stdout.strip(), "the hook printed nothing"
    out = json.loads(stdout)
    assert set(out) == {"systemMessage"}, out
    return out["systemMessage"]


def test_tracing_on_names_the_workspace_and_content(session_start, home, tmp_path):
    _sign_in(home)
    _enable(home, str(tmp_path))
    assert _message(session_start(home, str(tmp_path))) == (
        "Rius: tracing this session to workspace acme (content: on)")


def test_content_off_is_said(session_start, home, tmp_path):
    _sign_in(home)
    _enable(home, str(tmp_path))
    out = session_start(home, str(tmp_path),
                         env={"RIUS_CAPTURE_CONTENT": "false"})
    assert _message(out).endswith("(content: off)")


def test_an_env_key_has_no_workspace_name_to_show(session_start, home, tmp_path):
    _enable(home, str(tmp_path))
    out = session_start(home, str(tmp_path), env={"RIUS_API_KEY": "gf_k"})
    assert _message(out) == "Rius: tracing this session (content: on)"


def test_enabled_folder_without_sign_in_asks_for_login(session_start, home, tmp_path):
    _enable(home, str(tmp_path))
    assert _message(session_start(home, str(tmp_path))) == notice.SIGN_IN


def test_install_notice_is_shown_once_ever(session_start, home, tmp_path):
    first = session_start(home, str(tmp_path), sid="a")
    assert _message(first) == notice.INSTALLED
    assert session_start(home, str(tmp_path), sid="b") == ""


def test_signed_in_but_not_enabled_here_says_nothing(session_start, home, tmp_path):
    _sign_in(home)
    assert session_start(home, str(tmp_path)) == ""


def test_a_line_is_never_repeated_within_a_session(session_start, home, tmp_path):
    _sign_in(home)
    _enable(home, str(tmp_path))
    assert session_start(home, str(tmp_path))
    for source in ("resume", "compact", "clear"):
        assert session_start(home, str(tmp_path), source=source) == ""


def test_a_resumed_session_is_told_when_the_state_changed(session_start, home, tmp_path):
    _sign_in(home)
    _enable(home, str(tmp_path))
    assert session_start(home, str(tmp_path))
    out = session_start(home, str(tmp_path), source="resume",
                         env={"RIUS_CAPTURE_CONTENT": "false"})
    assert _message(out).endswith("(content: off)")


def test_a_continued_conversation_is_not_told_again(session_start, home, tmp_path):
    _sign_in(home)
    _enable(home, str(tmp_path))
    assert session_start(home, str(tmp_path), sid="old")
    st = state.load("new", home)
    st["continued_from"] = "old"
    state.save("new", home, st)
    assert session_start(home, str(tmp_path), sid="new") == ""


def test_other_events_print_nothing(home, tmp_path, monkeypatch, capsys):
    _sign_in(home)
    _enable(home, str(tmp_path))
    monkeypatch.setattr(hook_mod.subprocess, "Popen",
                        lambda *a, **k: None)
    monkeypatch.setattr(os, "environ", {"HOME": home, "USERPROFILE": home})
    monkeypatch.setattr(sys, "argv", ["hook.py", "UserPromptSubmit"])
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(
        {"session_id": SID, "cwd": str(tmp_path),
         "transcript_path": "/nonexistent.jsonl"})))
    hook_mod.main()
    assert capsys.readouterr().out == ""


# --- The workspace refusing data -------------------------------------------

ENV = {"RIUS_API_KEY": "gf_k", "RIUS_ENDPOINT": "https://ingest.test"}


def _export(home, monkeypatch, fixtures_dir, status, sid):
    monkeypatch.setattr(exporter.otlp, "export",
                        lambda ep, key, body, timeout=5.0: status)
    payload = {"session_id": sid, "cwd": "/tmp/proj",
               "transcript_path": str(fixtures_dir / "simple.jsonl")}
    exporter.run("PostToolUse", payload, ENV, home)


def test_402_notice_then_cleared_on_success(session_start, home, tmp_path, monkeypatch,
                                            fixtures_dir):
    _enable(home, "/tmp")
    _export(home, monkeypatch, fixtures_dir, 402, "s-refused")

    assert _message(session_start(home, "/tmp/proj", sid="next",
                                   env=ENV)) == notice.REFUSED
    printed = io.StringIO()
    with contextlib.redirect_stdout(printed):
        rius_ctl.dispatch(["status", "--session", "next", "--cwd", "/tmp/proj"],
                          home)
    assert notice.REFUSED in printed.getvalue()

    _export(home, monkeypatch, fixtures_dir, 200, "s-accepted")
    assert _message(session_start(home, "/tmp/proj", sid="next",
                                   env=ENV)).startswith("Rius: tracing")


def test_a_402_is_named_in_the_export_error(home, monkeypatch, fixtures_dir):
    _enable(home, "/tmp")
    _export(home, monkeypatch, fixtures_dir, 402, SID)
    assert "not accepting data" in state.load(SID, home)["last_export_error"][
        "reason"]


@pytest.mark.parametrize("status", [401, 403, 429, 500, 0])
def test_other_failures_are_not_a_refusal(home, monkeypatch, fixtures_dir,
                                          status):
    _enable(home, "/tmp")
    _export(home, monkeypatch, fixtures_dir, status, SID)
    cfg = config.resolve(SID, "/tmp/proj", ENV, home)
    assert not notice.is_refused(home, cfg)


def test_a_refusal_belongs_to_the_key_it_was_recorded_for(home, monkeypatch,
                                                          fixtures_dir):
    _enable(home, "/tmp")
    _export(home, monkeypatch, fixtures_dir, 402, SID)
    other = config.resolve(SID, "/tmp/proj", dict(ENV, RIUS_API_KEY="gf_new"),
                           home)
    assert not notice.is_refused(home, other)


def test_a_stopped_session_is_not_told_it_is_tracing(session_start, home,
                                                     tmp_path):
    """The exporter's stop is sticky: a conversation disabled after its trace
    started sends nothing more, even where the rules now say on."""
    _sign_in(home)
    _enable(home, str(tmp_path))
    st = state.load("new", home)
    st.update({"continued_from": "old", "content_stopped": True})
    state.save("new", home, st)
    assert session_start(home, str(tmp_path), sid="new") == ""


def test_month_old_records_are_pruned_and_this_sessions_kept(
        session_start, home, tmp_path):
    _sign_in(home)
    _enable(home, str(tmp_path))
    session_start(home, str(tmp_path), sid="old")
    session_start(home, str(tmp_path), sid="recent")
    session_start(home, str(tmp_path), sid="current")
    notices = os.path.join(home, ".claude", "rius", "notices")
    month_ago = time.time() - notice.SHOWN_RECORD_MAX_AGE_S - 60
    for sid in ("old", "current"):
        os.utime(os.path.join(notices, sid), (month_ago, month_ago))

    assert session_start(home, str(tmp_path), sid="current") == ""
    assert sorted(os.listdir(notices)) == ["current", "recent"]
