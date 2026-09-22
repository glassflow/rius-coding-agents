"""The platform seam, on BOTH branches.

The POSIX implementations run natively here. The Windows implementations are
driven with fakes, because this suite never runs on Windows -- these tests
are the only thing standing between a Windows user and the four traps in
`rius_cc.platform_compat`, so they are load-bearing, not illustrative.

The one that matters most is `test_windows_liveness_never_calls_os_kill`.
`os.kill(pid, 0)` is a harmless probe on POSIX and an attack on Windows, and
the target of that probe is the user's own Claude Code process.
"""
import os
import subprocess
import sys

import pytest

from rius_cc import platform_compat as pc


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeKernel32:
    """The three kernel32 calls `_windows_pid_alive` is allowed to make."""

    def __init__(self, handle=0x1234, exit_code=pc.STILL_ACTIVE,
                 get_exit_code_ok=True):
        self.handle = handle
        self.exit_code = exit_code
        self.get_exit_code_ok = get_exit_code_ok
        self.open_calls = []
        self.closed = []

    def OpenProcess(self, access, inherit, pid):
        self.open_calls.append((access, inherit, pid))
        return self.handle

    def GetExitCodeProcess(self, handle, out_ref):
        # ctypes.byref(x)._obj is x, so a fake can write through the pointer
        # exactly as the real API does.
        out_ref._obj.value = self.exit_code
        return 1 if self.get_exit_code_ok else 0

    def CloseHandle(self, handle):
        self.closed.append(handle)
        return 1


class FakeMsvcrt:
    """Windows byte-range locking, with its actual sharing semantics.

    A Windows lock belongs to the file HANDLE, not the process: a second
    descriptor on the same file is refused even inside one process. That is
    the property `test_lock_is_exclusive` depends on, so the fake keys its
    registry on the file's identity and its owner on the descriptor.
    """

    LK_NBLCK = 2
    LK_LOCK = 1
    LK_UNLCK = 0

    def __init__(self):
        self.held = {}          # (dev, ino) -> fd
        self.modes_used = []
        self.offsets = []

    def _key(self, fd):
        st = os.fstat(fd)
        return (st.st_dev, st.st_ino)

    def locking(self, fd, mode, nbytes):
        self.modes_used.append(mode)
        self.offsets.append(os.lseek(fd, 0, os.SEEK_CUR))
        key = self._key(fd)
        if mode == self.LK_UNLCK:
            if self.held.get(key) == fd:
                del self.held[key]
            return
        owner = self.held.get(key)
        if owner is not None and owner != fd:
            raise OSError(36, "Resource deadlock avoided")
        self.held[key] = fd


@pytest.fixture
def as_windows(monkeypatch):
    """Route every dispatcher down the Windows branch."""
    monkeypatch.setattr(pc, "IS_WINDOWS", True)
    return pc


@pytest.fixture
def fake_msvcrt(monkeypatch):
    """Install a fake `msvcrt` so the real `import msvcrt` inside the
    Windows lock functions resolves to it. That keeps the import itself --
    the exact thing that broke on the other platform -- under test."""
    fake = FakeMsvcrt()
    monkeypatch.setitem(sys.modules, "msvcrt", fake)
    return fake


# ---------------------------------------------------------------------------
# TRAP 1 -- process liveness
# ---------------------------------------------------------------------------

def test_windows_liveness_never_calls_os_kill(monkeypatch, as_windows):
    """THE test. On Windows `os.kill(pid, 0)` is not a probe.

    CPython's os_kill_impl branches on the signal value. CTRL_C_EVENT is 0
    in the Windows SDK, so signal 0 becomes
    GenerateConsoleCtrlEvent(CTRL_C_EVENT, pid) -- a Ctrl+C aimed at the
    console process group with that id, which is Claude Code. Without
    console IO the same call reaches TerminateProcess(handle, 0) instead.
    Either way the heartbeat would kill the session it is watching, so the
    Windows path must never reach os.kill at all.
    """
    def explode(*args, **kwargs):
        raise AssertionError(
            "os.kill reached on the Windows path -- this terminates or "
            "Ctrl+C's the user's Claude Code process")

    monkeypatch.setattr(os, "kill", explode)
    monkeypatch.setattr(pc, "_kernel32", lambda: FakeKernel32())

    assert pc.pid_alive(4321) is True


def test_windows_liveness_uses_a_read_only_access_mask(as_windows):
    k32 = FakeKernel32()
    pc._windows_pid_alive(99, kernel32=k32)
    assert len(k32.open_calls) == 1
    access, inherit, pid = k32.open_calls[0]
    # PROCESS_QUERY_LIMITED_INFORMATION and nothing else: not
    # PROCESS_ALL_ACCESS (0x1F0FFF), which carries PROCESS_TERMINATE and is
    # refused across integrity levels anyway.
    assert access == pc.PROCESS_QUERY_LIMITED_INFORMATION
    assert pid == 99
    # Not entailed by the mask: a probe has no business handing its handle
    # to the children it spawns, and the exporter/pinger are spawned from
    # processes that run this.
    assert inherit == 0, "the process handle must not be inheritable"


def test_windows_still_active_means_alive():
    k32 = FakeKernel32(exit_code=pc.STILL_ACTIVE)
    assert pc._windows_pid_alive(1, kernel32=k32) is True


def test_windows_exited_process_is_not_alive():
    k32 = FakeKernel32(exit_code=0)
    assert pc._windows_pid_alive(1, kernel32=k32) is False


def test_windows_handle_is_always_closed():
    for kwargs in ({}, {"get_exit_code_ok": False}, {"exit_code": 0}):
        k32 = FakeKernel32(**kwargs)
        pc._windows_pid_alive(1, kernel32=k32)
        assert k32.closed == [k32.handle], "leaked a process handle"


def test_windows_access_denied_means_alive():
    """Mirrors POSIX PermissionError: it exists, we just may not look."""
    k32 = FakeKernel32(handle=0)
    assert pc._windows_pid_alive(
        1, kernel32=k32, last_error=lambda: pc.ERROR_ACCESS_DENIED) is True


class BoomKernel32:
    """kernel32 that fails the way ctypes actually fails.

    `ctypes.ArgumentError` is NOT an OSError subclass, so nothing in the
    heartbeat's `except OSError` vocabulary catches it. An escape from here
    reaches `Pinger.run()`, is swallowed by heartbeat.py's module-tail
    `except BaseException: pass`, and the pinger dies mid-session with no
    stopped ping and no log -- this codebase's signature failure shape.
    """

    def __init__(self, exc):
        self.exc = exc

    def OpenProcess(self, *a):
        raise self.exc

    def GetExitCodeProcess(self, *a):
        raise self.exc

    def CloseHandle(self, *a):
        raise self.exc


@pytest.fixture
def quiet_home(tmp_path, monkeypatch):
    """Point the seam's own breadcrumb log at tmp, and reset its dedupe."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr(pc, "_WARNED", set())
    return tmp_path


def _log_lines(home):
    d = os.path.join(str(home), ".claude", "rius", "log")
    lines = []
    for name in sorted(os.listdir(d)) if os.path.isdir(d) else []:
        with open(os.path.join(d, name)) as fh:
            lines += [ln for ln in fh.read().splitlines() if ln.strip()]
    return lines


def test_windows_liveness_survives_a_raising_kernel32(quiet_home):
    """`_posix_pid_alive` is total by construction; this one must be too.

    Conservative on failure: "alive". Reporting dead would stop the pinger
    on a healthy session, which is the outage, not the safety.
    """
    import ctypes
    boom = BoomKernel32(ctypes.ArgumentError("argument 3: wrong type"))
    assert pc._windows_pid_alive(1, kernel32=boom) is True


def test_windows_liveness_survives_a_windll_that_cannot_load(quiet_home,
                                                             monkeypatch):
    """ctypes.WinDLL("kernel32") itself can raise OSError."""
    def no_kernel32():
        raise OSError("cannot load kernel32")
    monkeypatch.setattr(pc, "_kernel32", no_kernel32)
    assert pc._windows_pid_alive(4321) is True


def test_windows_liveness_leaves_a_breadcrumb_exactly_once(quiet_home):
    """Returning True quietly forever would hide a permanently broken probe,
    and the heartbeat would look healthy while measuring nothing. Once, not
    every 15 seconds: this is polled for the life of the session."""
    boom = BoomKernel32(ValueError("nope"))
    for _ in range(5):
        assert pc._windows_pid_alive(1, kernel32=boom) is True
    lines = _log_lines(quiet_home)
    assert len(lines) == 1, lines
    assert "pid_alive" in lines[0]


class BoomMsvcrt:
    LK_NBLCK = 2
    LK_LOCK = 1
    LK_UNLCK = 0

    def locking(self, fd, mode, nbytes):
        import ctypes
        raise ctypes.ArgumentError("not an OSError")


def test_windows_try_lock_survives_a_non_oserror(tmp_path, quiet_home,
                                                 monkeypatch):
    """A raising lock attempt must read as "contended", not crash the hook."""
    monkeypatch.setitem(sys.modules, "msvcrt", BoomMsvcrt())
    fd = pc.open_lock_file(str(tmp_path / "s.lock"))
    try:
        assert pc._windows_try_lock(fd) is False
    finally:
        os.close(fd)


def test_windows_unlock_survives_a_non_oserror(tmp_path, quiet_home,
                                               monkeypatch):
    monkeypatch.setitem(sys.modules, "msvcrt", BoomMsvcrt())
    fd = pc.open_lock_file(str(tmp_path / "s.lock"))
    try:
        pc._windows_unlock(fd)      # must not raise
    finally:
        os.close(fd)


def test_windows_invalid_pid_is_not_alive():
    k32 = FakeKernel32(handle=0)
    assert pc._windows_pid_alive(
        1, kernel32=k32, last_error=lambda: 87) is False   # INVALID_PARAMETER


def test_windows_unreadable_exit_code_assumes_alive():
    """The handle opened, so the process exists. Guessing 'dead' here stops
    the heartbeat on a live session."""
    k32 = FakeKernel32(get_exit_code_ok=False)
    assert pc._windows_pid_alive(1, kernel32=k32) is True


def test_posix_liveness_natively():
    assert pc.pid_alive(os.getpid()) is True
    assert pc.pid_alive(999999) is False


def test_posix_permission_error_means_alive():
    def denied(pid, sig):
        raise PermissionError(1, "not yours")
    assert pc._posix_pid_alive(1, kill=denied) is True


def test_pid_zero_is_never_probed(monkeypatch):
    """os.kill(0, 0) on POSIX addresses the caller's whole process group,
    and 0 is how parent_pid_of spells 'unknown'."""
    def explode(*args, **kwargs):
        raise AssertionError("probed pid 0")
    monkeypatch.setattr(os, "kill", explode)
    assert pc.pid_alive(0) is False
    assert pc.pid_alive(-1) is False


# ---------------------------------------------------------------------------
# TRAP 2 -- file locking
# ---------------------------------------------------------------------------

def test_windows_lock_is_exclusive_between_descriptors(tmp_path, fake_msvcrt):
    """Same guarantee as test_lock_is_exclusive, on the Windows branch."""
    path = str(tmp_path / "s.lock")
    a = pc.open_lock_file(path)
    b = pc.open_lock_file(path)
    try:
        assert pc._windows_try_lock(a) is True
        assert pc._windows_try_lock(b) is False
    finally:
        os.close(a)
        os.close(b)


def test_windows_unlock_lets_the_next_descriptor_in(tmp_path, fake_msvcrt):
    path = str(tmp_path / "s.lock")
    a = pc.open_lock_file(path)
    b = pc.open_lock_file(path)
    try:
        assert pc._windows_try_lock(a) is True
        pc._windows_unlock(a)
        assert pc._windows_try_lock(b) is True
    finally:
        os.close(a)
        os.close(b)


def test_windows_lock_is_non_blocking_and_never_lk_lock(tmp_path, fake_msvcrt):
    """LK_LOCK has its own fixed 10-retries-at-1s behaviour. session_lock
    promises a deadline the CALLER chose, so the retry loop must stay in
    session_lock and the primitive must stay non-blocking."""
    path = str(tmp_path / "s.lock")
    fd = pc.open_lock_file(path)
    try:
        pc._windows_try_lock(fd)
    finally:
        os.close(fd)
    assert fake_msvcrt.modes_used == [FakeMsvcrt.LK_NBLCK]
    assert FakeMsvcrt.LK_LOCK not in fake_msvcrt.modes_used


def test_windows_lock_always_seeks_to_zero_first(tmp_path, fake_msvcrt):
    """msvcrt.locking locks from the CURRENT file position. Without the
    seek two callers could lock two different bytes and both think they won."""
    path = str(tmp_path / "s.lock")
    fd = pc.open_lock_file(path)
    try:
        os.write(fd, b"junk that moves the file position")
        pc._windows_try_lock(fd)
        pc._windows_unlock(fd)
    finally:
        os.close(fd)
    assert fake_msvcrt.offsets == [0, 0]


def test_windows_unlock_never_raises(tmp_path, fake_msvcrt):
    fd = pc.open_lock_file(str(tmp_path / "s.lock"))
    os.close(fd)
    pc._windows_unlock(fd)          # bad descriptor: must be swallowed


def test_posix_lock_natively(tmp_path):
    path = str(tmp_path / "s.lock")
    a = pc.open_lock_file(path)
    b = pc.open_lock_file(path)
    try:
        assert pc._posix_try_lock(a) is True
        assert pc._posix_try_lock(b) is False
    finally:
        os.close(a)
        os.close(b)


# ---------------------------------------------------------------------------
# TRAP 3 -- detached spawn
# ---------------------------------------------------------------------------

def test_posix_detach_uses_start_new_session():
    assert pc.detached_child_kwargs() == {"start_new_session": True}


def test_windows_detach_uses_creationflags_not_start_new_session(as_windows):
    kwargs = pc.detached_child_kwargs()
    assert "start_new_session" not in kwargs, \
        "start_new_session is POSIX-only; on Windows it is rejected or ignored"
    flags = kwargs["creationflags"]
    assert flags & pc.DETACHED_PROCESS
    assert flags & pc.CREATE_NEW_PROCESS_GROUP


# ---------------------------------------------------------------------------
# TRAP 5 -- parent pid
# ---------------------------------------------------------------------------

def test_posix_parent_pid_natively():
    assert pc.parent_pid_of(os.getpid()) == os.getppid()


def test_posix_parent_pid_returns_zero_when_ps_fails():
    def boom(*a, **kw):
        raise OSError("no ps here")
    assert pc._posix_parent_pid_of(123, check_output=boom) == 0


def test_windows_parent_pid_from_a_process_snapshot(as_windows):
    snapshot = lambda: [(1, 0), (500, 1), (777, 500)]   # noqa: E731
    assert pc._windows_parent_pid_of(777, pairs=snapshot) == 500


def test_windows_parent_pid_unknown_pid_is_zero(as_windows):
    snapshot = lambda: [(1, 0), (500, 1)]               # noqa: E731
    assert pc._windows_parent_pid_of(999, pairs=snapshot) == 0


def test_windows_parent_pid_rejects_zero_and_self_parents(as_windows):
    """0 is not a watchable process and a self-parent is a corrupt row.
    Returning either would have the pinger watch the wrong thing."""
    assert pc._windows_parent_pid_of(4, pairs=lambda: [(4, 0)]) == 0
    assert pc._windows_parent_pid_of(4, pairs=lambda: [(4, 4)]) == 0


def test_windows_parent_pid_survives_a_failing_snapshot(as_windows):
    def boom():
        raise OSError("CreateToolhelp32Snapshot failed")
    assert pc._windows_parent_pid_of(4, pairs=boom) == 0


def test_parent_pid_of_zero_is_zero():
    assert pc.parent_pid_of(0) == 0


# ---------------------------------------------------------------------------
# Home directory
# ---------------------------------------------------------------------------

def test_posix_home_prefers_env_home():
    assert pc.home_dir({"HOME": "/home/kiran"}) == "/home/kiran"


def test_posix_home_falls_back_to_expanduser():
    assert pc.home_dir({}) == os.path.expanduser("~")


def test_windows_rejects_a_git_bash_msys_home(as_windows):
    """Git Bash sets HOME=/c/Users/me. Handed to a native python.exe that
    resolves against the current drive root, so hook.py and exporter.py would
    read and write state in two different places and neither would say so."""
    env = {"HOME": "/c/Users/me", "USERPROFILE": "C:\\Users\\me"}
    assert pc.home_dir(env) == "C:\\Users\\me"


def test_windows_keeps_a_native_home_override(as_windows):
    """The test suite and env-scoped installs both override HOME."""
    env = {"HOME": "D:\\tmp\\home", "USERPROFILE": "C:\\Users\\me"}
    assert pc.home_dir(env) == "D:\\tmp\\home"


def test_windows_accepts_a_unc_home(as_windows):
    env = {"HOME": "\\\\server\\share\\me"}
    assert pc.home_dir(env) == "\\\\server\\share\\me"


# ---------------------------------------------------------------------------
# Atomic replace
# ---------------------------------------------------------------------------

def test_posix_replace_overwrites(tmp_path):
    src, dst = tmp_path / "a", tmp_path / "b"
    src.write_text("new")
    dst.write_text("old")
    pc.replace_atomic(str(src), str(dst))
    assert dst.read_text() == "new"
    assert not src.exists()


def test_windows_replace_retries_a_briefly_locked_destination(as_windows):
    """os.replace DOES overwrite on Windows, but it fails while another
    process holds the destination open -- and /rius status reads state.json
    without the lock."""
    attempts = {"n": 0}

    def flaky(src, dst):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise PermissionError(13, "used by another process")

    pc.replace_atomic("a", "b", sleep=lambda _d: None, replace=flaky)
    assert attempts["n"] == 3


def test_windows_replace_gives_up_and_raises(as_windows):
    def always(src, dst):
        raise PermissionError(13, "used by another process")

    with pytest.raises(OSError):
        pc.replace_atomic("a", "b", attempts=3, sleep=lambda _d: None,
                          replace=always)


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------

def test_describe_names_the_live_implementation(as_windows):
    assert "msvcrt" in pc.describe()
    assert "OpenProcess" in pc.describe()


def test_platform_compat_imports_no_platform_module_at_module_scope():
    """`import fcntl` at module scope is exactly how this plugin failed on
    Windows: the detached exporter died on import with stderr on DEVNULL."""
    import ast
    import pathlib

    src = pathlib.Path(pc.__file__).read_text()
    tree = ast.parse(src)
    top_level = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            top_level += [a.name.split(".")[0] for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            top_level.append(node.module.split(".")[0])
    for banned in ("fcntl", "msvcrt", "winreg", "ctypes"):
        assert banned not in top_level, \
            "%s must be imported lazily, inside the branch that needs it" % banned


def test_every_shipped_module_imports_without_fcntl(monkeypatch):
    """Simulates Windows: no fcntl anywhere. Every shipped module must still
    import, because the detached exporter's stderr goes to DEVNULL and an
    ImportError there is invisible -- which is exactly how this plugin used
    to fail on Windows."""
    import builtins
    import importlib

    real_import = builtins.__import__

    def no_fcntl(name, *args, **kwargs):
        if name == "fcntl":
            raise ImportError("No module named 'fcntl'")
        return real_import(name, *args, **kwargs)

    shipped = ("rius_cc.platform_compat", "rius_cc.state", "rius_cc.config",
               "rius_cc.log", "rius_cc.otlp", "rius_cc.proto",
               "rius_cc.spans", "rius_cc.subagents", "rius_cc.transcript")
    # Reimporting rebinds BOTH sys.modules and the rius_cc package's
    # attributes, and other test modules already hold references to the
    # originals -- `from rius_cc import platform_compat` reads the package
    # attribute, so restoring sys.modules alone is not enough. Put both
    # back, or this test quietly changes what its neighbours are testing.
    import rius_cc

    saved = {name: sys.modules.get(name) for name in shipped}
    monkeypatch.setattr(builtins, "__import__", no_fcntl)
    try:
        for name in shipped:
            sys.modules.pop(name, None)
            importlib.import_module(name)
    finally:
        for name, module in saved.items():
            leaf = name.split(".")[-1]
            if module is not None:
                sys.modules[name] = module
                setattr(rius_cc, leaf, module)
            else:
                sys.modules.pop(name, None)
