import os

import pytest

from rius_cc import state


def test_new_state_shape():
    s = state.new_state()
    assert s["offset"] == 0
    assert s["open_tools"] == {}
    assert s["root_started"] is False


def test_load_missing_returns_new(tmp_path):
    assert state.load("nope", str(tmp_path))["offset"] == 0


def test_save_then_load_roundtrip(tmp_path):
    s = state.new_state()
    s["offset"] = 42
    s["open_tools"]["toolu_1"] = {"span_id": "aabb", "start_ns": 1}
    state.save("s1", str(tmp_path), s)
    back = state.load("s1", str(tmp_path))
    assert back["offset"] == 42
    assert back["open_tools"]["toolu_1"]["span_id"] == "aabb"


def test_corrupt_state_resets_instead_of_raising(tmp_path):
    state.save("s1", str(tmp_path), state.new_state())
    path = state.state_path("s1", str(tmp_path))
    with open(path, "w") as fh:
        fh.write("{broken")
    assert state.load("s1", str(tmp_path))["offset"] == 0


def test_save_is_atomic_leaving_no_temp_files(tmp_path):
    state.save("s1", str(tmp_path), state.new_state())
    d = os.path.dirname(state.state_path("s1", str(tmp_path)))
    assert [f for f in os.listdir(d) if f.endswith(".tmp")] == []


def test_lock_is_exclusive(tmp_path):
    home = str(tmp_path)
    with state.session_lock("s1", home) as got:
        assert got is True
        with state.session_lock("s1", home) as second:
            assert second is False


def test_lock_released_after_block(tmp_path):
    home = str(tmp_path)
    with state.session_lock("s1", home) as got:
        assert got is True
    with state.session_lock("s1", home) as again:
        assert again is True


def test_different_sessions_do_not_block_each_other(tmp_path):
    home = str(tmp_path)
    with state.session_lock("s1", home) as a:
        with state.session_lock("s2", home) as b:
            assert a is True and b is True


class _FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def test_block_timeout_retries_then_gives_up(tmp_path):
    """I4: SessionEnd is the LAST event for a session. If it loses the lock
    to a still-running Stop exporter -- and Stop and SessionEnd fire close
    together -- finalize_session never runs and the root span stays pending
    forever. A bounded wait, never an unbounded one."""
    home = str(tmp_path)
    clock, sleeps = _FakeClock(), []

    def sleep(dt):
        sleeps.append(dt)
        clock.advance(dt)

    with state.session_lock("s1", home):
        with state.session_lock("s1", home, block_timeout=2.0,
                                clock=clock, sleep=sleep) as got:
            assert got is False
    assert sleeps, "gave up without waiting at all"
    assert sum(sleeps) <= 2.0 + state.RETRY_INTERVAL_S, "waited longer than asked"


def test_block_timeout_acquires_when_the_holder_releases(tmp_path):
    home = str(tmp_path)
    clock, holder = _FakeClock(), {}

    def sleep(dt):
        clock.advance(dt)
        # the other exporter finishes partway through the wait
        if clock.t >= 0.2 and "cm" in holder:
            holder.pop("cm").__exit__(None, None, None)

    cm = state.session_lock("s1", home)
    cm.__enter__()
    holder["cm"] = cm
    with state.session_lock("s1", home, block_timeout=2.0,
                            clock=clock, sleep=sleep) as got:
        assert got is True


def test_default_is_non_blocking(tmp_path):
    home = str(tmp_path)
    clock, sleeps = _FakeClock(), []
    with state.session_lock("s1", home):
        with state.session_lock("s1", home, clock=clock,
                                sleep=sleeps.append) as got:
            assert got is False
    assert sleeps == []


def test_an_oserror_inside_the_with_body_is_not_masked(tmp_path):
    """The yield used to sit inside `except OSError`, so an OSError raised in
    the caller's body resumed the generator and yielded a second time ->
    RuntimeError("generator didn't stop after throw()"), hiding the real
    error."""
    home = str(tmp_path)
    with pytest.raises(OSError) as exc:
        with state.session_lock("s1", home):
            raise OSError("the real problem")
    assert "the real problem" in str(exc.value)
