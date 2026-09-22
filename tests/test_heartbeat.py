import json
import os
import re
import sys
import pathlib

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "scripts"))

import heartbeat  # noqa: E402
from rius_cc import state  # noqa: E402


# --- URL construction --------------------------------------------------

def test_url_bare_endpoint_gets_exactly_one_slash():
    assert heartbeat.heartbeat_url("https://ingest.staging.rius.glassflow.xyz") == \
        "https://ingest.staging.rius.glassflow.xyz/v1/heartbeat"


def test_url_trailing_slash_endpoint_gets_exactly_one_slash():
    assert heartbeat.heartbeat_url("https://ingest.staging.rius.glassflow.xyz/") == \
        "https://ingest.staging.rius.glassflow.xyz/v1/heartbeat"
    assert "//v1/heartbeat" not in heartbeat.heartbeat_url(
        "https://ingest.staging.rius.glassflow.xyz/")


# --- Payload contract ----------------------------------------------------

def test_payload_has_exact_v1_keys_no_stopped():
    p = heartbeat.build_payload("inst-1", "claude-code")
    assert set(p.keys()) == {
        "v", "instance_id", "agent_name", "sent_at", "sdk_language",
        "sdk_version", "open_traces", "open_trace_count",
    }
    assert "stopped" not in p


def test_payload_stopped_true_only_on_final_ping():
    p = heartbeat.build_payload("inst-1", "claude-code", stopped=True)
    assert p["stopped"] is True


def test_payload_values():
    p = heartbeat.build_payload("inst-1", "claude-code")
    assert p["v"] == 1
    assert p["instance_id"] == "inst-1"
    assert p["agent_name"] == "claude-code"
    assert p["sdk_language"] == "python"
    assert p["open_traces"] == []
    assert p["open_trace_count"] == 0


def test_sent_at_is_rfc3339_millis_with_z_suffix():
    p = heartbeat.build_payload("inst-1", "claude-code")
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$", p["sent_at"])


# --- Pinger lifecycle ------------------------------------------------------

class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def _stop_file(tmp_path):
    return os.path.join(str(tmp_path), "home", ".claude", "rius", "state",
                        "s1.heartbeat.stop")


def _stop_writer(tmp_path, sent, after=1, boom=False):
    """Transport that asks the pinger to stop after `after` pings.

    The stop file must be written by something OUTSIDE the pinger while it is
    already running -- that is what SessionEnd does. Pre-writing it before
    run() would be testing a stale file, which the pinger now deliberately
    clears (C2).
    """
    path = _stop_file(tmp_path)

    def transport(payload, timeout):
        sent.append(payload)
        if len(sent) >= after and not payload.get("stopped"):
            with open(path, "w") as fh:
                fh.write("")
        if boom:
            raise OSError("boom")

    return transport


def _make_pinger(tmp_path, transport, watch_pid=None, **kw):
    home = str(tmp_path / "home")
    clock = FakeClock()
    sleeps = []

    def sleep(dt):
        sleeps.append(dt)
        clock.advance(dt)

    pinger = heartbeat.Pinger(
        session_id="s1", home=home, instance_id="inst-1", agent_name="claude-code",
        transport=transport, watch_pid=watch_pid if watch_pid is not None else os.getpid(),
        clock=clock, sleep=sleep, **kw,
    )
    return pinger, clock, sleeps


def test_first_ping_is_immediate(tmp_path):
    sent = []
    pinger, clock, _ = _make_pinger(
        tmp_path, _stop_writer(tmp_path, sent), interval=15.0,
    )
    pinger.run()
    assert len(sent) >= 1
    assert sent[0].get("stopped") is None


def test_stop_file_triggers_final_ping_then_exit(tmp_path):
    sent = []
    pinger, clock, _ = _make_pinger(tmp_path, _stop_writer(tmp_path, sent))
    pinger.run()
    assert sent[-1]["stopped"] is True


def test_stale_stop_file_is_cleared_at_startup_and_never_lies(tmp_path):
    """C2: hook.py writes the stop file and NOTHING used to delete it. A
    pinger for a resumed session would find the old file on its first
    iteration and report a live agent as stopped."""
    sent = []
    pinger, clock, _ = _make_pinger(
        tmp_path, lambda p, t: sent.append(p),
        max_lifetime=1.0, interval=100.0, poll_interval=0.5,
    )
    os.makedirs(os.path.dirname(pinger.stop_path()), exist_ok=True)
    with open(pinger.stop_path(), "w") as fh:
        fh.write("")                       # left over from a previous session

    pinger.run()

    assert not os.path.exists(pinger.stop_path()), "stale stop file survived startup"
    assert len(sent) >= 1
    assert all(not p.get("stopped") for p in sent), \
        "reported stopped:true for a session that had only just started"


def test_a_live_pinger_does_not_clear_another_pingers_stop_file(tmp_path):
    """The startup clear happens only after the pid lock is won -- otherwise
    a duplicate pinger would swallow the stop signal meant for the live one."""
    sent = []
    pinger, clock, _ = _make_pinger(tmp_path, lambda p, t: sent.append(p))
    os.makedirs(os.path.dirname(pinger.pid_path()), exist_ok=True)
    with open(pinger.pid_path(), "w") as fh:
        fh.write(str(os.getpid()))         # a live pinger already owns this
    with open(pinger.stop_path(), "w") as fh:
        fh.write("")
    pinger.run()
    assert sent == []
    assert os.path.exists(pinger.stop_path())


def test_dead_parent_exits_without_stopped_ping(tmp_path):
    sent = []
    # A pid that (almost certainly) does not exist.
    dead_pid = 999999
    pinger, clock, _ = _make_pinger(tmp_path, lambda p, t: sent.append(p), watch_pid=dead_pid)
    pinger.run()
    assert len(sent) >= 1          # the immediate first ping still went out
    assert all(not p.get("stopped") for p in sent)


def test_max_lifetime_cap_exits_without_stopped_ping(tmp_path):
    """The cap bounds the PINGER's lifetime, not the agent's. A stopped
    ping at the cap would falsely assert a still-running agent had
    stopped -- exactly the lie the dead-parent case already avoids."""
    sent = []
    pinger, clock, sleeps = _make_pinger(
        tmp_path, lambda p, t: sent.append(p),
        max_lifetime=1.0, interval=100.0, poll_interval=0.5,
    )
    pinger.run()
    assert len(sent) >= 1  # the immediate first ping still went out
    assert all(not p.get("stopped") for p in sent)


def test_periodic_pings_at_interval(tmp_path):
    sent = []
    calls = {"n": 0}

    def transport(p, t):
        sent.append(p)
        calls["n"] += 1
        if calls["n"] >= 3:
            # simulate stop after a couple of periodic pings
            with open(pinger.stop_path(), "w") as fh:
                fh.write("")

    pinger, clock, _ = _make_pinger(
        tmp_path, transport, interval=15.0, poll_interval=1.0, max_lifetime=1000.0,
    )
    pinger.run()
    # first (immediate) + at least one periodic + final stopped
    non_stopped = [p for p in sent if not p.get("stopped")]
    assert len(non_stopped) >= 2
    assert sent[-1]["stopped"] is True


def test_failed_ping_is_never_retried(tmp_path):
    attempts = []
    pinger, clock, _ = _make_pinger(
        tmp_path, _stop_writer(tmp_path, attempts, boom=True))
    pinger.run()  # must not raise
    # exactly: first ping + final ping, no retries in between
    assert len(attempts) == 2


def test_failed_ping_is_logged(tmp_path):
    """I1: a pinger built without a logger swallows every delivery failure,
    which is exactly what made C1 impossible to diagnose."""
    logged = []
    sent = []
    pinger, clock, _ = _make_pinger(
        tmp_path, _stop_writer(tmp_path, sent, boom=True),
        log=logged.append)
    pinger.run()
    assert logged, "a failed heartbeat delivery logged nothing"
    assert "boom" in " ".join(logged)


def test_pid_lock_prevents_duplicate_pinger(tmp_path):
    sent = []
    pinger, clock, _ = _make_pinger(tmp_path, lambda p, t: sent.append(p))
    with open(pinger.pid_path(), "w") as fh:
        fh.write(str(os.getpid()))  # a live pid (this test process)
    pinger.run()
    assert sent == []  # exited immediately, never pinged


def test_stale_pid_lock_is_taken_over(tmp_path):
    sent = []
    pinger, clock, _ = _make_pinger(tmp_path, _stop_writer(tmp_path, sent))
    os.makedirs(os.path.dirname(pinger.pid_path()), exist_ok=True)
    with open(pinger.pid_path(), "w") as fh:
        fh.write("999999")  # not alive
    pinger.run()
    assert len(sent) >= 1


def test_pid_lock_released_on_exit(tmp_path):
    sent = []
    pinger, clock, _ = _make_pinger(tmp_path, _stop_writer(tmp_path, sent))
    pinger.run()
    assert not os.path.exists(pinger.pid_path())


# --- main() wiring: reads instance_id from persisted state, never invents one

@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    (h / ".claude" / "rius").mkdir(parents=True)
    with open(str(h / ".claude" / "rius" / "config.json"), "w") as fh:
        json.dump({"enabled_paths": ["/tmp"]}, fh)
    return str(h)


def test_main_uses_instance_id_from_state_not_a_fresh_uuid(monkeypatch, home):
    sid = "22222222-2222-2222-2222-222222222222"
    st = state.new_state()
    st["instance_id"] = "persisted-instance-id"
    state.save(sid, home, st)

    captured = {}

    class FakePinger:
        def __init__(self, session_id, home, instance_id, agent_name, transport,
                     watch_pid, **kw):
            captured["instance_id"] = instance_id
            captured["agent_name"] = agent_name

        def run(self):
            pass

    monkeypatch.setattr(heartbeat, "Pinger", FakePinger)
    monkeypatch.setattr(sys, "argv", ["heartbeat.py", sid, "/tmp/proj", home, str(os.getpid())])
    monkeypatch.setenv("RIUS_API_KEY", "glassflow_k")
    monkeypatch.setenv("RIUS_ENDPOINT", "https://ingest.test")
    heartbeat.main()
    assert captured["instance_id"] == "persisted-instance-id"


def test_main_does_nothing_without_persisted_instance_id(monkeypatch, home):
    sid = "33333333-3333-3333-3333-333333333333"
    # no state saved -- no instance_id

    called = {"n": 0}

    class FakePinger:
        def __init__(self, *a, **kw):
            called["n"] += 1

        def run(self):
            pass

    monkeypatch.setattr(heartbeat, "Pinger", FakePinger)
    monkeypatch.setattr(sys, "argv", ["heartbeat.py", sid, "/tmp/proj", home, str(os.getpid())])
    monkeypatch.setenv("RIUS_API_KEY", "glassflow_k")
    monkeypatch.setenv("RIUS_ENDPOINT", "https://ingest.test")
    heartbeat.main()
    assert called["n"] == 0


def test_main_accepts_the_instance_id_minted_by_hook_on_argv(monkeypatch, home):
    """C1, production ordering: hook.py mints the id and passes it on argv,
    so the pinger is usable with NO state file at all -- it no longer races
    the exporter for the one thing it cannot invent."""
    sid = "55555555-5555-5555-5555-555555555555"
    assert not state.load(sid, home).get("instance_id")   # nothing persisted

    captured = {}

    class FakePinger:
        def __init__(self, session_id, home, instance_id, agent_name, transport,
                     watch_pid, **kw):
            captured["instance_id"] = instance_id

        def run(self):
            pass

    monkeypatch.setattr(heartbeat, "Pinger", FakePinger)
    monkeypatch.setattr(sys, "argv", ["heartbeat.py", sid, "/tmp/proj", home,
                                      str(os.getpid()), "minted-by-hook"])
    monkeypatch.setenv("RIUS_API_KEY", "glassflow_k")
    monkeypatch.setenv("RIUS_ENDPOINT", "https://ingest.test")
    heartbeat.main()
    assert captured["instance_id"] == "minted-by-hook"


def test_main_logs_why_it_refuses_to_start(monkeypatch, home, tmp_path):
    """I1: both silent returns in main() used to log nothing even with
    RIUS_CLAUDE_DEBUG=true, contradicting the module docstring."""
    sid = "66666666-6666-6666-6666-666666666666"
    monkeypatch.setattr(sys, "argv", ["heartbeat.py", sid, "/tmp/proj", home,
                                      str(os.getpid())])
    monkeypatch.setenv("RIUS_API_KEY", "glassflow_supersecret")
    monkeypatch.setenv("RIUS_ENDPOINT", "https://ingest.test")
    monkeypatch.setenv("RIUS_CLAUDE_DEBUG", "true")
    heartbeat.main()   # no instance id anywhere -> refuses

    logs = list(pathlib.Path(home, ".claude", "rius", "log").glob("*.log"))
    assert logs, "the pinger refused to start and said nothing"
    text = "\n".join(p.read_text() for p in logs)
    assert "instance_id" in text
    assert "supersecret" not in text


def test_main_logs_when_disabled(monkeypatch, home):
    sid = "77777777-7777-7777-7777-777777777777"
    monkeypatch.setattr(sys, "argv", ["heartbeat.py", sid, "/nowhere", home,
                                      str(os.getpid()), "inst-1"])
    monkeypatch.setenv("RIUS_API_KEY", "glassflow_k")
    monkeypatch.setenv("RIUS_CLAUDE_DEBUG", "true")
    heartbeat.main()
    logs = list(pathlib.Path(home, ".claude", "rius", "log").glob("*.log"))
    assert logs and "\n".join(p.read_text() for p in logs).strip()


def test_main_does_nothing_when_disabled(monkeypatch, home):
    sid = "44444444-4444-4444-4444-444444444444"
    st = state.new_state()
    st["instance_id"] = "persisted-instance-id"
    state.save(sid, home, st)

    called = {"n": 0}

    class FakePinger:
        def __init__(self, *a, **kw):
            called["n"] += 1

        def run(self):
            pass

    monkeypatch.setattr(heartbeat, "Pinger", FakePinger)
    # cwd not in enabled_paths -> disabled by default
    monkeypatch.setattr(sys, "argv", ["heartbeat.py", sid, "/nowhere", home, str(os.getpid())])
    monkeypatch.setenv("RIUS_API_KEY", "glassflow_k")
    monkeypatch.setenv("RIUS_ENDPOINT", "https://ingest.test")
    heartbeat.main()
    assert called["n"] == 0


# --- Cross-platform liveness ------------------------------------------------

from rius_cc import platform_compat  # noqa: E402


def test_liveness_never_uses_os_kill_on_windows(tmp_path, monkeypatch):
    """TRAP 1, at the call site that would do the damage. os.kill(pid, 0) is
    a probe on POSIX and an attack on Windows: CTRL_C_EVENT is 0, so signal
    0 becomes GenerateConsoleCtrlEvent(CTRL_C_EVENT, pid) aimed at Claude
    Code's console process group, and on a build without console IO it
    reaches TerminateProcess instead. The pinger must never get there."""
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)

    def explode(*args, **kwargs):
        raise AssertionError("the heartbeat signalled the process it watches")

    monkeypatch.setattr(os, "kill", explode)
    monkeypatch.setattr(platform_compat, "_kernel32",
                        lambda: _FakeKernel32Alive())

    sent = []
    pinger, _clock, _sleeps = _make_pinger(
        tmp_path, _stop_writer(tmp_path, sent), watch_pid=4321)
    pinger.run()
    assert any(p.get("stopped") for p in sent)


class _FakeKernel32Alive:
    def OpenProcess(self, access, inherit, pid):
        assert access == platform_compat.PROCESS_QUERY_LIMITED_INFORMATION
        return 0x99

    def GetExitCodeProcess(self, handle, out_ref):
        out_ref._obj.value = platform_compat.STILL_ACTIVE
        return 1

    def CloseHandle(self, handle):
        return 1


def test_unwatched_pinger_keeps_running_instead_of_exiting(tmp_path, monkeypatch):
    """TRAP 5's degradation. When the parent cannot be identified hook.py
    passes 0. The pinger must NOT treat that as a dead parent and leave --
    that is a session that silently produces no heartbeats. It runs on the
    stop file and the lifetime cap instead, and says so."""
    probed = []
    monkeypatch.setattr(platform_compat, "pid_alive",
                        lambda pid: probed.append(pid) or True)

    sent, logged = [], []
    pinger, _clock, _sleeps = _make_pinger(
        tmp_path, _stop_writer(tmp_path, sent, after=2), watch_pid=0,
        interval=0.0, log=logged.append)
    pinger.run()

    assert len(sent) >= 2, "the unwatched pinger exited after its first ping"
    assert any(p.get("stopped") for p in sent), "no final stopped ping"
    assert probed == [], "pid 0 must never be probed"
    assert any("unwatched" in m for m in logged), \
        "running unwatched was not reported anywhere"
