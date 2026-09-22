import multiprocessing
import os
import time

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
