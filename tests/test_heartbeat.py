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
        tmp_path, lambda p, t: sent.append(p),
        interval=15.0,
    )
    # write stop file right away so run() exits after first ping + final ping
    with open(pinger.stop_path(), "w") as fh:
        fh.write("")
    pinger.run()
    assert len(sent) >= 1
    assert sent[0].get("stopped") is None


def test_stop_file_triggers_final_ping_then_exit(tmp_path):
    sent = []
    pinger, clock, _ = _make_pinger(tmp_path, lambda p, t: sent.append(p))
    with open(pinger.stop_path(), "w") as fh:
        fh.write("")
    pinger.run()
    assert sent[-1]["stopped"] is True


def test_dead_parent_exits_without_stopped_ping(tmp_path):
    sent = []
    # A pid that (almost certainly) does not exist.
    dead_pid = 999999
    pinger, clock, _ = _make_pinger(tmp_path, lambda p, t: sent.append(p), watch_pid=dead_pid)
    pinger.run()
    assert all(not p.get("stopped") for p in sent)


def test_max_lifetime_cap_sends_stopped_ping(tmp_path):
    sent = []
    pinger, clock, sleeps = _make_pinger(
        tmp_path, lambda p, t: sent.append(p),
        max_lifetime=1.0, interval=100.0, poll_interval=0.5,
    )
    pinger.run()
    assert sent[-1]["stopped"] is True


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
    attempts = {"n": 0}

    def flaky_transport(p, t):
        attempts["n"] += 1
        raise OSError("boom")

    pinger, clock, _ = _make_pinger(tmp_path, flaky_transport)
    with open(pinger.stop_path(), "w") as fh:
        fh.write("")
    pinger.run()  # must not raise
    # exactly: first ping + final ping, no retries in between
    assert attempts["n"] == 2


def test_pid_lock_prevents_duplicate_pinger(tmp_path):
    sent = []
    pinger, clock, _ = _make_pinger(tmp_path, lambda p, t: sent.append(p))
    with open(pinger.pid_path(), "w") as fh:
        fh.write(str(os.getpid()))  # a live pid (this test process)
    pinger.run()
    assert sent == []  # exited immediately, never pinged


def test_stale_pid_lock_is_taken_over(tmp_path):
    sent = []
    pinger, clock, _ = _make_pinger(tmp_path, lambda p, t: sent.append(p))
    with open(pinger.pid_path(), "w") as fh:
        fh.write("999999")  # not alive
    with open(pinger.stop_path(), "w") as fh:
        fh.write("")
    pinger.run()
    assert len(sent) >= 1


def test_pid_lock_released_on_exit(tmp_path):
    pinger, clock, _ = _make_pinger(tmp_path, lambda p, t: None)
    with open(pinger.stop_path(), "w") as fh:
        fh.write("")
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
