"""The suite-wide guard against tests writing into the real state dir."""
from tests.conftest import leaked


def test_a_test_session_file_is_flagged():
    assert leaked(["x.json"], ["x.json", "s1.json", "s1.lock"]) == ["s1.json", "s1.lock"]
    assert leaked([], ["aaaaaaaa-0000-0000-0000-000000000001.json"]) == [
        "aaaaaaaa-0000-0000-0000-000000000001.json"]


def test_a_live_session_writing_meanwhile_is_not():
    live = "c8471b29-eb0d-44db-8b7a-1b4fc1601b59"
    assert leaked([], [live + ".json", live + ".lock",
                       live + ".heartbeat.pid"]) == []


def test_files_already_there_are_not_flagged():
    assert leaked(["s1.json"], ["s1.json"]) == []
