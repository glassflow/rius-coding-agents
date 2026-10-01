"""The platform layer against the real OS, with no fakes.

Every Windows branch in platform_compat is also tested with injected fakes
so it runs on Linux; those prove the wiring, not the OS. These run against
whatever the suite is on, so the `windows` CI job is where they turn the
fakes' assumptions into observations.
"""
import os
import subprocess
import sys
import threading
import time

from rius_cc import platform_compat as pc
from rius_cc import state
from tests.platforms import windows_only

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_HOLDER = """
import sys
sys.path.insert(0, sys.argv[1])
from rius_cc import state
with state.session_lock("s1", sys.argv[2]) as got:
    print("held" if got else "refused", flush=True)
    sys.stdin.readline()
"""


def test_the_session_lock_excludes_another_process(tmp_path):
    """Two exporters for one session must not both win. On Windows this is
    msvcrt byte-range locking between two real processes, which the fakes
    can only model."""
    home = str(tmp_path)
    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, os.path.join(ROOT, "scripts"), home],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        with state.session_lock("s1", home) as got:
            assert got is False, "a second process took a held session lock"
    finally:
        holder.communicate("\n", timeout=30)
    with state.session_lock("s1", home) as got:
        assert got is True, "the lock outlived the process that held it"


def test_an_exited_child_is_not_alive():
    child = subprocess.Popen([sys.executable, "-c", ""])
    child.wait(timeout=30)
    assert pc.pid_alive(child.pid) is False


def test_a_running_child_is_alive_and_its_parent_is_us():
    child = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"],
                             stdin=subprocess.PIPE)
    try:
        assert pc.pid_alive(child.pid) is True
        assert pc.parent_pid_of(child.pid) == os.getpid()
    finally:
        child.communicate(b"", timeout=30)


@windows_only("os.replace only refuses an open destination on Windows")
def test_replace_waits_out_a_reader_holding_the_destination(tmp_path):
    """/rius status reads state.json without the session lock. A plain
    os.replace onto a file another handle has open fails on Windows; the
    bounded retry is what makes state.save survive that reader."""
    src, dst = tmp_path / "new", tmp_path / "state.json"
    src.write_text("new")
    dst.write_text("old")
    reader = open(str(dst))
    threading.Timer(0.05, reader.close).start()
    pc.replace_atomic(str(src), str(dst))
    assert dst.read_text() == "new"


def test_a_detached_child_outlives_its_parent(tmp_path):
    """The exporter has to keep running after the hook that spawned it
    exits. tests/test_e2e.py shows the export arriving; this shows the
    process itself surviving its parent."""
    marker = tmp_path / "survived"
    spawn = (
        "import subprocess, sys\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "from rius_cc import platform_compat as pc\n"
        "child = subprocess.Popen([sys.executable, '-c',\n"
        "    'import time, sys; time.sleep(1); open(sys.argv[1], \"w\").close()',\n"
        "    sys.argv[2]], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,\n"
        "    stderr=subprocess.DEVNULL, **pc.detached_child_kwargs())\n"
        "print(child.pid)\n")
    parent = subprocess.run(
        [sys.executable, "-c", spawn, os.path.join(ROOT, "scripts"), str(marker)],
        capture_output=True, text=True, timeout=30, check=True)
    assert int(parent.stdout) > 0
    deadline = time.time() + 15
    while not marker.exists() and time.time() < deadline:
        time.sleep(0.05)
    assert marker.exists(), "the detached child died with its parent"
