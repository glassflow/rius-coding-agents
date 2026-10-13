"""The parts of the live Cursor harness (tests/live) that need no Cursor: what
it expects of the spans, when it refuses to start, and that it puts nothing in
the home that it does not take out again."""
import gzip
import json
import os
import pathlib
import sys
import urllib.error
import urllib.request

import pytest

LIVE = pathlib.Path(__file__).parent / "live"
FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "cursor_live"
sys.path.insert(0, str(LIVE))

import cursor_live  # noqa: E402
import dump_hook  # noqa: E402
import harness  # noqa: E402
import receiver  # noqa: E402
import trace_shape as shape  # noqa: E402

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="needs symlinks")


def load(name):
    lines = (FIXTURES / (name + ".jsonl")).read_text(encoding="utf-8").splitlines()
    return shape.merge(shape.decode(lines))


def test_each_recorded_run_meets_its_scenarios_expectations():
    assert shape.check_shell(load("shell")) == []
    assert shape.check_subagent(load("subagent")) == []
    assert shape.check_resume(load("resume")) == []
    assert shape.check_content(load("content")) == []


def test_a_run_with_no_failing_shell_call_is_refused():
    spans = load("shell")
    for span in spans:
        span["status"] = 1
    assert any("ERROR" in p for p in shape.check_shell(spans))


def test_a_run_where_every_shell_call_failed_is_refused():
    spans = load("shell")
    for span in spans:
        if span["kind"] == "TOOL":
            span["status"] = shape.ERROR
    assert any("every Shell" in p for p in shape.check_shell(spans))


def test_content_from_a_structure_only_folder_is_refused():
    assert any("content attributes" in p for p in shape.check_shell(load("content")))


def test_a_structure_only_run_does_not_meet_the_content_scenario():
    problems = shape.check_content(load("shell"))
    assert "no TOOL span has input.value" in problems
    assert "no TOOL span has output.value" in problems


def test_a_run_without_a_subagent_is_refused():
    assert "no Task TOOL span" in shape.check_subagent(load("shell"))


def test_a_resume_that_added_no_turn_is_refused():
    assert any("second turn" in p for p in shape.check_resume(load("shell")))


def test_a_resume_that_started_a_new_trace_is_refused():
    spans = load("resume")
    spans[-1]["trace_id"] = "f" * 32
    assert any("new trace" in p for p in shape.check_resume(spans))


def test_another_service_name_is_refused():
    spans = load("shell")
    spans[0]["resource"] = {"service.name": "codex"}
    assert any("service.name" in p for p in shape.check_shell(spans))


def test_no_spans_is_one_clear_problem():
    assert shape.NO_SPAN in shape.check_shell([])
    assert shape.common([]) == [shape.NO_SPAN]


def test_a_root_that_never_ended_is_refused_unless_the_session_is_still_open():
    spans = load("shell")
    next(s for s in spans if s["kind"] == "AGENT")["pending"] = True
    assert any("never finished" in p for p in shape.check_shell(spans))
    assert shape.common(spans, finished=False) == []


def span_line(span_id, pending):
    attrs = [{"key": "glassflow.span.pending", "value": {"boolValue": pending}},
             {"key": "gen_ai.usage.input_tokens", "value": {"intValue": "12"}}]
    return json.dumps({"resourceSpans": [{"resource": {"attributes": []}, "scopeSpans": [
        {"spans": [{"name": "n", "spanId": span_id, "attributes": attrs}]}]}]})


@pytest.mark.parametrize("order", [(True, False), (False, True)])
def test_the_finished_copy_of_a_span_wins_whichever_arrived_first(order):
    lines = [span_line("a1", pending) for pending in order]
    (merged,) = shape.merge(shape.decode(lines))
    assert merged["pending"] is False


def test_int_values_are_decoded_from_their_decimal_strings():
    (span,) = shape.decode([span_line("a1", False)])
    assert span["attrs"]["gen_ai.usage.input_tokens"] == 12


def llm(**attrs):
    return {"kind": "LLM", "attrs": attrs}


def test_tokens_are_required_only_when_asked():
    assert shape.check_tokens([llm()], required=False) == []
    assert shape.check_tokens([], required=False) == ["no LLM span"]
    assert shape.check_tokens([llm()], required=True)
    counted = llm(**{shape.INPUT_TOKENS: 5, shape.OUTPUT_TOKENS: 0})
    assert shape.check_tokens([counted], required=True)
    counted = llm(**{shape.INPUT_TOKENS: 5, shape.OUTPUT_TOKENS: 2})
    assert shape.check_tokens([counted], required=True) == []


def test_an_interactive_run_needs_a_prompt_and_a_stop_in_the_spool():
    names = shape.spool_events(['{"event":"beforeSubmitPrompt"}', "not json", '{"event":"stop"}'])
    assert shape.check_interactive_events(names) == []
    assert shape.check_interactive_events(["stop"]) == ["no beforeSubmitPrompt event in the spool"]


def test_the_dump_hook_keeps_key_names_and_numbers_but_no_text():
    out = dump_hook.summary({"status": "completed", "text": "secret", "input_tokens": 7,
                             "flag": True})
    assert out == {"keys": ["flag", "input_tokens", "status", "text"],
                   "numbers": {"input_tokens": 7}}


def test_the_model_is_the_override_else_the_first_cheap_one_listed():
    listing = "Available models\n\nauto - Auto\ncomposer-2.5 - Composer\ngpt-5.2 - GPT\n"
    assert cursor_live.choose_model(listing) == "composer-2.5"
    assert cursor_live.choose_model(listing, "gpt-5.2") == "gpt-5.2"
    assert cursor_live.choose_model("nothing useful") == "auto"


def test_escape_codes_are_taken_out_of_what_the_terminal_drew():
    assert cursor_live.strip_ansi("\x1b[1mRius tracing\x1b[0m: on\r\n\x1b]0;title\x07") \
        == "Rius tracing: on\n"


def test_list_prints_the_seven_scenarios(capsys):
    assert cursor_live.main(["cursor_live.py", "list"]) == 0
    assert len(capsys.readouterr().out.split()) == 7


def test_an_unknown_scenario_is_a_usage_error(capsys):
    assert cursor_live.main(["cursor_live.py", "nope"]) == 2


def test_the_real_home_is_not_resolved_while_the_suite_marker_exists():
    with pytest.raises(harness.Abort):
        cursor_live.real_layout()


class FakeCtl:
    """rius_ctl.py as it behaves: exit 0 always, its effect in the home."""

    def __init__(self, home):
        self.layout = harness.Layout(str(home / "home"), str(home / "root"))
        self.calls = []
        self.contents = set()

    def __call__(self, args, stdin):
        self.calls.append((args, stdin))
        action = args[0]
        if action == "use-key":
            pathlib.Path(self.layout.rius, harness.CREDENTIALS).write_text("{}")
        elif action == "install-hooks":
            pathlib.Path(self.layout.hooks_json).write_text('{"hooks":{}}')
        elif action == "content-on-here":
            self.contents.add(args[-1])
        elif action == "status":
            content = harness.CONTENT_ON if args[-1] in self.contents else harness.CONTENT_OFF
            return 0, "%s\n%s\n" % (harness.TRACING_ON, content)
        return 0, ""


@pytest.fixture
def home(tmp_path):
    (tmp_path / "home" / ".cursor").mkdir(parents=True)
    (tmp_path / "home" / ".cursor" / "hooks.json.rius-test-removed").write_text("keep")
    (tmp_path / "root").mkdir()
    (tmp_path / "scratch").mkdir()
    return tmp_path


def layout_of(home):
    return harness.Layout(str(home / "home"), str(home / "root"))


def listing(path):
    return sorted(p.relative_to(path).as_posix() for p in path.rglob("*"))


def test_a_clean_home_with_a_logged_in_cursor_may_start(home):
    assert harness.preflight(layout_of(home), "✓ Logged in as someone") == []


def test_the_suite_marker_stops_the_harness(home):
    layout = layout_of(home)
    pathlib.Path(layout.marker).parent.mkdir(parents=True)
    pathlib.Path(layout.marker).write_text("")
    assert any("marker" in p for p in harness.preflight(layout, "Logged in"))


def test_a_rius_dir_the_harness_did_not_make_stops_it(home):
    layout = layout_of(home)
    os.makedirs(layout.rius)
    assert any("may hold a real key" in p for p in harness.preflight(layout, "Logged in"))


def test_a_rius_dir_left_by_an_earlier_run_points_at_cleanup(home):
    layout = layout_of(home)
    os.makedirs(layout.rius)
    pathlib.Path(layout.sentinel).write_text("{}")
    assert any("cleanup" in p for p in harness.preflight(layout, "Logged in"))


@posix_only
def test_an_installed_plugin_stops_the_harness_even_when_the_link_dangles(home):
    layout = layout_of(home)
    os.makedirs(layout.plugins_local)
    os.symlink(str(home / "nowhere"), layout.link)
    assert any("installed" in p for p in harness.preflight(layout, "Logged in"))


def test_a_cursor_that_is_not_logged_in_stops_the_harness(home):
    assert any("not logged in" in p
               for p in harness.preflight(layout_of(home), "✗ Not logged in"))


@posix_only
def test_setup_links_the_checkout_stores_the_dummy_key_and_enables_both_folders(home):
    ctl = FakeCtl(home)
    session = harness.Session(layout_of(home), ctl, str(home / "scratch"))
    session.setup()
    layout = session.layout
    assert os.path.realpath(layout.link) == os.path.realpath(layout.root)
    assert ctl.calls[0] == (["use-key", "--agent", "cursor", "--env", "local"], harness.DUMMY_KEY)
    assert ctl.calls[1][0] == ["enable-here", "--agent", "cursor", "--cwd", session.structure]
    assert ctl.calls[2][0][:2] == ["status", "--agent"]
    assert ctl.calls[3][0] == ["content-on-here", "--agent", "cursor", "--cwd", session.content]
    assert session.structure != session.content
    assert session.structure == os.path.realpath(session.structure)
    session.teardown(settle=0)


@posix_only
def test_teardown_leaves_the_home_as_it_found_it(home):
    before = listing(home)
    session = harness.Session(layout_of(home), FakeCtl(home), str(home / "scratch"))
    session.setup()
    session.claim_hooks_json()
    assert session.teardown(settle=0) == []
    assert listing(home) == before
    assert (home / "home" / ".cursor" / "hooks.json.rius-test-removed").read_text() == "keep"


@posix_only
def test_teardown_keeps_the_plugin_dirs_that_were_already_there(home):
    os.makedirs(home / "home" / ".cursor" / "plugins" / "other")
    before = listing(home)
    session = harness.Session(layout_of(home), FakeCtl(home), str(home / "scratch"))
    session.setup()
    assert session.teardown(settle=0) == []
    assert listing(home) == before


@posix_only
def test_teardown_removes_a_dir_the_exporter_wrote_after_the_first_removal(home, monkeypatch):
    session = harness.Session(layout_of(home), FakeCtl(home), str(home / "scratch"))
    session.setup()

    def exporter_writes_late(_seconds):
        os.makedirs(os.path.join(session.layout.rius, "spool"), exist_ok=True)
        pathlib.Path(session.layout.rius, "spool", "c.jsonl").write_text("{}\n")

    monkeypatch.setattr(harness.time, "sleep", exporter_writes_late)
    assert session.teardown(settle=1) == []
    assert not os.path.exists(session.layout.rius)


def test_teardown_without_the_sentinel_removes_nothing(home):
    layout = layout_of(home)
    os.makedirs(layout.rius)
    pathlib.Path(layout.rius, "credentials.json").write_text("a real key")
    assert harness.teardown(layout, settle=0) == []
    assert pathlib.Path(layout.rius, "credentials.json").read_text() == "a real key"


@posix_only
def test_a_hooks_file_that_is_already_there_is_never_changed(home):
    layout = layout_of(home)
    pathlib.Path(layout.hooks_json).write_text('{"version":1,"hooks":{}}')
    session = harness.Session(layout, FakeCtl(home), str(home / "scratch"))
    session.setup()
    with pytest.raises(harness.Abort):
        session.add_hook("stop", "x")
    with pytest.raises(harness.Abort):
        session.install_hooks()
    assert session.teardown(settle=0) == []
    assert pathlib.Path(layout.hooks_json).read_text() == '{"version":1,"hooks":{}}'


@posix_only
def test_a_hooks_file_the_harness_made_is_removed_with_everything_in_it(home):
    session = harness.Session(layout_of(home), FakeCtl(home), str(home / "scratch"))
    session.setup()
    session.add_hook("stop", "dump")
    session.add_hook("stop", "again")
    doc = json.loads(pathlib.Path(session.layout.hooks_json).read_text())
    assert doc["hooks"]["stop"] == [{"command": "dump"}, {"command": "again"}]
    assert session.teardown(settle=0) == []
    assert not os.path.lexists(session.layout.hooks_json)


@posix_only
def test_a_failing_rius_ctl_aborts_the_setup_and_teardown_still_cleans_up(home):
    before = listing(home)
    session = harness.Session(layout_of(home), lambda a, s: (1, "boom"), str(home / "scratch"))
    with pytest.raises(harness.Abort):
        session.setup()
    assert session.teardown(settle=0) == []
    assert listing(home) == before


@posix_only
def test_a_rius_ctl_that_exits_zero_and_stores_no_key_aborts_the_setup(home):
    before = listing(home)
    session = harness.Session(layout_of(home), lambda a, s: (0, "Rius: not done."),
                              str(home / "scratch"))
    with pytest.raises(harness.Abort, match="stored no key"):
        session.setup()
    assert session.teardown(settle=0) == []
    assert listing(home) == before


@posix_only
def test_a_folder_that_is_not_traced_after_enable_aborts_the_setup(home):
    class Refusing(FakeCtl):
        def __call__(self, args, stdin):
            if args[0] == "status":
                return 0, "Rius tracing: off\n"
            return super().__call__(args, stdin)

    session = harness.Session(layout_of(home), Refusing(home), str(home / "scratch"))
    with pytest.raises(harness.Abort, match="did not turn tracing on"):
        session.setup()
    assert session.teardown(settle=0) == []


@posix_only
def test_a_content_folder_that_traces_structure_only_aborts_the_setup(home):
    class Ignoring(FakeCtl):
        def __call__(self, args, stdin):
            if args[0] == "content-on-here":
                return 0, ""
            return super().__call__(args, stdin)

    session = harness.Session(layout_of(home), Ignoring(home), str(home / "scratch"))
    with pytest.raises(harness.Abort, match="did not turn tracing on"):
        session.setup()
    assert session.teardown(settle=0) == []


@posix_only
def test_install_hooks_that_wrote_no_hooks_file_aborts(home):
    class Silent(FakeCtl):
        def __call__(self, args, stdin):
            return (0, "") if args[0] == "install-hooks" else super().__call__(args, stdin)

    session = harness.Session(layout_of(home), Silent(home), str(home / "scratch"))
    session.setup()
    with pytest.raises(harness.Abort, match="wrote no"):
        session.install_hooks()
    assert session.teardown(settle=0) == []


@posix_only
@pytest.mark.parametrize("failing", ["wait_quiet", "collect"])
@pytest.mark.parametrize("error", [KeyboardInterrupt, OSError])
def test_teardown_undoes_everything_when_the_copy_out_fails(home, monkeypatch, failing, error):
    before = listing(home)
    session = harness.Session(layout_of(home), FakeCtl(home), str(home / "scratch"))
    session.setup()

    def fail(*_args):
        raise error()

    monkeypatch.setattr(harness, failing, fail)
    with pytest.raises(error):
        session.teardown(str(home / "scratch"), settle=0)
    assert listing(home) == before
    assert not os.path.lexists(session.layout.link)


@posix_only
def test_the_link_is_recorded_before_it_is_made(home, monkeypatch):
    session = harness.Session(layout_of(home), FakeCtl(home), str(home / "scratch"))

    def die(*_args):
        assert harness._read_state(session.layout)["link"] == session.layout.link
        raise KeyboardInterrupt

    monkeypatch.setattr(harness.os, "symlink", die)
    with pytest.raises(KeyboardInterrupt):
        session.setup()
    assert session.teardown(settle=0) == []


@posix_only
def test_an_interrupted_run_process_kills_the_command_it_started(tmp_path, monkeypatch):
    started = []
    real_popen = cursor_live.subprocess.Popen

    class Recording(real_popen):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            started.append(self)

        def communicate(self, *args, **kwargs):
            raise KeyboardInterrupt

    monkeypatch.setattr(cursor_live.subprocess, "Popen", Recording)
    with pytest.raises(KeyboardInterrupt):
        cursor_live.run_process([sys.executable, "-c", "import time; time.sleep(60)"],
                                str(tmp_path))
    assert started[0].poll() is not None


def test_the_spool_and_the_logs_are_copied_out_before_they_go(home, tmp_path):
    rius = tmp_path / "rius"
    (rius / "spool").mkdir(parents=True)
    (rius / "log").mkdir()
    (rius / "spool" / "c.jsonl").write_text("{}\n")
    (rius / "spool" / "c.parent").write_text("p")
    (rius / "log" / "hook.log").write_text("line")
    run = tmp_path / "run"
    run.mkdir()
    harness.collect(str(rius), str(run))
    assert listing(run) == ["log", "log/hook.log", "spool", "spool/c.jsonl"]


def test_waiting_for_quiet_returns_once_nothing_changes(tmp_path):
    (tmp_path / "f").write_text("x")
    harness.wait_quiet(str(tmp_path), quiet=0.3, maximum=5)


def post(port, path, body, headers=None):
    request = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), data=body,
                                     headers=headers or {}, method="POST")
    return urllib.request.urlopen(request, timeout=5)


def test_the_receiver_keeps_every_trace_body_it_is_sent(tmp_path):
    rcv = receiver.Receiver(str(tmp_path), port=0)
    rcv.start()
    try:
        port = rcv._server.server_address[1]
        assert post(port, "/v1/traces", b'{"resourceSpans":[]}').status == 200
        zipped = gzip.compress(b'{"resourceSpans":[1]}')
        assert post(port, "/v1/traces", zipped, {"Content-Encoding": "gzip"}).status == 200
        with pytest.raises(urllib.error.HTTPError) as other:
            post(port, "/v1/metrics", b"{}")
        assert other.value.code == 404
        with pytest.raises(urllib.error.HTTPError) as bad:
            post(port, "/v1/traces", b"not json")
        assert bad.value.code == 400
    finally:
        rcv.stop()
    assert rcv.lines() == ['{"resourceSpans":[]}', '{"resourceSpans":[1]}']
    assert rcv.requests == 2


def test_a_busy_port_is_reported_not_taken_over(tmp_path):
    first = receiver.Receiver(str(tmp_path), port=0)
    try:
        with pytest.raises(receiver.PortBusy):
            receiver.Receiver(str(tmp_path), port=first._server.server_address[1])
    finally:
        first._server.server_close()
