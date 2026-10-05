"""RIUS-1223: where a key may be sent, and who can read what the plugin
keeps on disk."""
import contextlib
import io
import json
import os
import stat
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from rius_cc import config, continuation, log, login, net, otlp, platform_compat, state
from tests.platforms import posix_only
from tests.test_hook import _enabled_env
from tests.test_rius_ctl import _run as _run_ctl

import exporter  # noqa: E402  (scripts/ is on pythonpath)
import heartbeat  # noqa: E402
import hook as hook_mod  # noqa: E402


# --- https only ---------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "https://ingest.rius-glassflow.com/v1/traces",
    "http://localhost:4318/v1/traces",
    "http://127.0.0.1:8080/x",
    "http://[::1]:8080/x",
])
def test_https_and_loopback_http_are_allowed(url):
    assert net.is_allowed_url(url)


@pytest.mark.parametrize("url", [
    "http://ingest.example.com/v1/traces",
    "http://127.0.0.1.evil.example/x",
    "ftp://ingest.example.com/",
    "file:///etc/passwd",
    "https:///no-host",
    "ingest.example.com/v1/traces",
])
def test_other_urls_are_refused(url):
    assert not net.is_allowed_url(url)


def test_a_refused_url_never_reaches_the_network(monkeypatch):
    def opened(*_a, **_kw):
        raise AssertionError("opened a refused URL")
    monkeypatch.setattr(net._OPENER, "open", opened)
    req = urllib.request.Request("http://ingest.example.com/v1/traces",
                                 headers={"Authorization": "Bearer ri_k"})
    with pytest.raises(urllib.error.URLError):
        net.urlopen(req, timeout=1)


def test_export_to_plain_http_sends_nothing_and_does_not_retry(monkeypatch):
    sleeps = []
    monkeypatch.setattr(net._OPENER, "open", lambda *_a, **_kw: pytest.fail("sent"))
    status = otlp.export("http://ingest.example.com", "ri_k", b"x",
                         sleep=sleeps.append)
    assert status == 0
    assert sleeps == []


# --- no redirects -------------------------------------------------------------

@contextlib.contextmanager
def _server(handler_cls):
    srv = HTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=srv.serve_forever,
                              kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    try:
        yield srv, "http://127.0.0.1:%d" % srv.server_address[1]
    finally:
        srv.shutdown()
        srv.server_close()


class _Recorder(BaseHTTPRequestHandler):
    seen = []

    def _record(self):
        type(self).seen.append(self.headers.get("Authorization"))
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    do_GET = do_POST = _record

    def log_message(self, *_a):
        pass


@contextlib.contextmanager
def _redirecting_to_recorder(code=302):
    _Recorder.seen = []
    with _server(_Recorder) as (_, target):
        class Redirect(BaseHTTPRequestHandler):
            def do_POST(self):
                self.send_response(code)
                self.send_header("Location", target + "/stolen")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *_a):
                pass

        with _server(Redirect) as (_, base):
            yield base


@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
def test_otlp_export_does_not_follow_a_redirect(code):
    with _redirecting_to_recorder(code) as base:
        status = otlp.export(base, "ri_secret", b"x", sleep=lambda _s: None)
    assert status == code
    assert _Recorder.seen == []


def test_heartbeat_does_not_follow_a_redirect():
    with _redirecting_to_recorder() as base:
        send = heartbeat.http_transport(base + "/v1/heartbeat", "ri_secret")
        with pytest.raises(urllib.error.HTTPError) as err:
            send({"instance_id": "i"}, timeout=5)
    assert err.value.code == 302
    assert _Recorder.seen == []


def test_login_requests_do_not_follow_a_redirect():
    """Poll and revoke both go through post_json; revoke carries the key."""
    with _redirecting_to_recorder() as base:
        status, _ = login.post_json(base + "/v1/agent-keys/revoke",
                                    {"workspace_id": "w"}, "ri_secret")
    assert status == 302
    assert _Recorder.seen == []


def test_a_redirect_is_reported_as_a_redirect():
    reason = exporter._export_error_reason(302)
    assert "redirect" in reason and "not followed" in reason


# --- owner-only files ---------------------------------------------------------

def _mode(path):
    return stat.S_IMODE(os.stat(str(path)).st_mode)


@posix_only("mode bits")
def test_rius_dir_and_its_subdirs_are_owner_only(tmp_path):
    path = platform_compat.rius_dir(str(tmp_path), "state")
    assert _mode(tmp_path / ".claude" / "rius") == 0o700
    assert _mode(path) == 0o700


@posix_only("mode bits")
def test_an_existing_open_rius_dir_is_tightened(tmp_path):
    for d in (tmp_path / ".claude" / "rius", tmp_path / ".claude" / "rius" / "log"):
        d.mkdir(parents=True, exist_ok=True)
        d.chmod(0o755)
    platform_compat.rius_dir(str(tmp_path), "log")
    assert _mode(tmp_path / ".claude" / "rius") == 0o700
    assert _mode(tmp_path / ".claude" / "rius" / "log") == 0o700


@posix_only("mode bits")
def test_log_files_are_owner_only(tmp_path):
    cfg = type("Cfg", (), {"debug": True, "api_key": None})()
    log.write(str(tmp_path), cfg, "hello")
    (logfile,) = (tmp_path / ".claude" / "rius" / "log").iterdir()
    assert _mode(logfile) == 0o600


@posix_only("mode bits")
def test_lock_files_are_owner_only(tmp_path):
    with state.session_lock("s1", str(tmp_path)):
        pass
    assert _mode(state.lock_path("s1", str(tmp_path))) == 0o600


@posix_only("mode bits")
def test_session_overrides_live_in_an_owner_only_dir(tmp_path):
    config.set_session_override("s1", str(tmp_path), True)
    assert _mode(tmp_path / ".claude" / "rius" / "sessions") == 0o700


def test_no_chmod_on_windows(tmp_path, monkeypatch):
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(os, "chmod", lambda *_a: pytest.fail("chmod on Windows"))
    path = platform_compat.rius_dir(str(tmp_path), "state")
    assert os.path.isdir(path)


# --- session ids --------------------------------------------------------------

@pytest.mark.parametrize("sid", [
    "0f6a3c1e-8d7b-4e2a-9c5f-1b2d3e4f5a6b", "s1", "A-b-9"])
def test_valid_session_ids(sid):
    assert state.is_valid_session_id(sid)


@pytest.mark.parametrize("sid", [
    "", "../../x", "a/b", "a\\b", "a.b", "s1\n", " s1", "x" * 65, None, 7,
    "..", "C:x"])
def test_invalid_session_ids(sid):
    assert not state.is_valid_session_id(sid)


def test_no_file_path_is_built_from_an_invalid_session_id(tmp_path):
    for build in (state.state_path, state.lock_path, state.open_marker_path,
                  config.session_override_path):
        with pytest.raises(ValueError):
            build("../../x", str(tmp_path))


def test_config_resolve_shrugs_off_an_invalid_session_id(tmp_path):
    cfg = config.resolve("../../x", str(tmp_path), {}, str(tmp_path))
    assert cfg is not None


def test_ctl_refuses_a_path_shaped_session_id(tmp_path):
    home = tmp_path / "home"
    (home / ".claude" / "rius").mkdir(parents=True)
    r = _run_ctl(["on", "--session", "../../escaped"], str(home))
    assert r.returncode == 0
    assert "not a session id" in r.stdout
    assert not (home / ".claude" / "escaped").exists()
    assert os.listdir(str(home / ".claude")) == ["rius"]


def test_hook_ignores_a_path_shaped_session_id(tmp_path, monkeypatch):
    env, home = _enabled_env(tmp_path)
    spawned = []
    monkeypatch.setattr(hook_mod.subprocess, "Popen",
                        lambda *a, **kw: spawned.append(a))
    monkeypatch.setattr(os, "environ", dict(env))
    monkeypatch.setattr(sys, "argv", ["hook.py", "SessionEnd"])
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(
        {"session_id": "../../escaped", "cwd": str(tmp_path),
         "transcript_path": "/x.jsonl"})))
    hook_mod.main()
    assert spawned == []
    assert not (tmp_path / "home" / ".claude" / "escaped.heartbeat.stop").exists()


def test_exporter_ignores_a_path_shaped_session_id(tmp_path):
    assert exporter.run("Stop", {"session_id": "../x", "cwd": str(tmp_path),
                                 "transcript_path": "/x.jsonl"},
                        {}, str(tmp_path)) == 0
    assert not (tmp_path / ".claude").exists()


def test_a_continued_id_from_a_transcript_is_not_trusted(tmp_path):
    assert not continuation._was_traced("../../etc/passwd", str(tmp_path))


# --- enable-here refuses $HOME and above --------------------------------------

def _enable(tmp_path, folder):
    home = tmp_path / "home"
    (home / ".claude" / "rius").mkdir(parents=True, exist_ok=True)
    r = _run_ctl(["enable-here", "--cwd", str(folder)], str(home))
    rules = config.read_path_rules(str(home))
    return r, config.rule_list(rules, "enabled_paths")


def test_enable_here_refuses_the_home_folder(tmp_path):
    r, enabled = _enable(tmp_path, tmp_path / "home")
    assert "home folder" in r.stdout
    assert enabled == []


def test_enable_here_refuses_a_parent_of_home(tmp_path):
    r, enabled = _enable(tmp_path, tmp_path)
    assert "home folder" in r.stdout
    assert enabled == []


def test_enable_here_still_takes_a_project_inside_home(tmp_path):
    project = tmp_path / "home" / "code" / "proj"
    project.mkdir(parents=True)
    r, enabled = _enable(tmp_path, project)
    assert "home folder" not in r.stdout
    assert enabled == [config.resolved(str(project))]


def test_a_sibling_whose_name_starts_like_home_is_not_its_parent(tmp_path):
    sibling = tmp_path / "home-projects"
    sibling.mkdir()
    r, enabled = _enable(tmp_path, sibling)
    assert enabled == [config.resolved(str(sibling))]


# --- the hook payload never lingers --------------------------------------------

def _payload_files(d):
    return [n for n in os.listdir(str(d)) if n.startswith("rius-hook-")]


def test_payload_is_deleted_when_the_exporter_cannot_start(tmp_path, monkeypatch):
    env, home = _enabled_env(tmp_path)
    spool = tmp_path / "tmp"
    spool.mkdir()
    monkeypatch.setattr(hook_mod.tempfile, "tempdir", str(spool))

    def no_spawn(argv, **_kw):
        raise OSError("cannot spawn")
    monkeypatch.setattr(hook_mod.subprocess, "Popen", no_spawn)
    monkeypatch.setattr(os, "environ", dict(env))
    monkeypatch.setattr(sys, "argv", ["hook.py", "PostToolUse"])
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(
        {"session_id": "s1", "cwd": str(tmp_path),
         "transcript_path": "/x.jsonl", "tool_response": "secret"})))
    hook_mod.main()
    assert _payload_files(spool) == []


def test_exporter_deletes_a_payload_it_cannot_parse(tmp_path, monkeypatch):
    bad = tmp_path / "rius-hook-bad.json"
    bad.write_text("{not json")
    monkeypatch.setattr(sys, "argv", ["exporter.py", str(bad)])
    with pytest.raises(SystemExit):
        exporter.main()
    assert not bad.exists()


def test_status_skips_a_state_file_that_is_not_a_session(tmp_path):
    home = tmp_path / "home"
    state_dir = home / ".claude" / "rius" / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "s1.json").write_text("{}")
    os.utime(str(state_dir / "s1.json"), (1, 1))
    (state_dir / "not a session.json").write_text("{}")
    r = _run_ctl(["status", "--cwd", str(tmp_path)], str(home))
    assert r.returncode == 0
    assert "error" not in r.stdout
    assert "session: s1 (inferred" in r.stdout
