"""Every place this plugin has to care which operating system it is on.

The plugin used to be POSIX-only in a way that failed SILENTLY on Windows:
``state.py`` imported ``fcntl`` at module scope, so the detached exporter --
whose stderr goes to DEVNULL -- died on import before it could log anything.
Hooks still exited 0, and nothing was ever traced.

Rather than scatter ``if sys.platform == "win32"`` through the codebase, each
difference lives here as three pieces:

* ``_posix_x(...)``   -- the POSIX implementation, exercised natively.
* ``_windows_x(...)`` -- the Windows implementation. Its OS dependencies are
  arguments with lazy defaults, so the branch can be unit-tested with fakes
  on Linux and macOS, where the suite actually runs.
* ``x(...)``          -- a dispatcher on ``IS_WINDOWS`` and nothing else.

Standard library only (``msvcrt``, ``ctypes`` and ``winreg`` are stdlib), and
Python 3.9 compatible. Windows-only modules are imported INSIDE functions:
importing ``msvcrt`` at module scope would break POSIX exactly the way
``fcntl`` broke Windows.
"""
from __future__ import annotations

import os
import re
import stat
import subprocess
import sys
import time

from . import agent

IS_WINDOWS = sys.platform.startswith("win")

# --- Win32 constants (winnt.h / winbase.h / tlhelp32.h) ---------------------
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259
ERROR_ACCESS_DENIED = 5
TH32CS_SNAPPROCESS = 0x00000002
MAX_PROCESSES = 8192

# subprocess only defines these on Windows, so they are spelled out; the
# values are from the CreateProcess dwCreationFlags documentation.
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200

# os.replace on Windows fails while another process holds the destination
# open (Python's open() does not pass FILE_SHARE_DELETE). Readers here hold
# state.json open for microseconds, so a short bounded retry covers it.
REPLACE_ATTEMPTS = 10
REPLACE_DELAY_S = 0.02

_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")

PS_TIMEOUT_S = 1.0


def describe() -> str:
    """One line for ``/rius:status``.

    A Windows user whose plugin is quietly doing nothing needs SOME surface
    that says which code path is live. This is it.
    """
    if IS_WINDOWS:
        return "%s (locking: msvcrt, liveness: OpenProcess)" % sys.platform
    return "%s (locking: fcntl.flock, liveness: os.kill(pid, 0))" % sys.platform


# ---------------------------------------------------------------------------
# Home directory
# ---------------------------------------------------------------------------

def is_native_absolute(path: str) -> bool:
    """True if `path` is absolute in THIS platform's spelling.

    On Windows that means a drive (``C:\\x``) or a UNC share
    (``\\\\server\\share``). It deliberately does NOT mean ``/c/Users/me``,
    which is what Git Bash puts in ``$HOME``: handed to a native python.exe
    that resolves against the current drive's root, not the user's profile.
    """
    if not path:
        return False
    if not IS_WINDOWS:
        return path.startswith("/")
    if _WINDOWS_DRIVE_RE.match(path):
        return True
    return path.startswith("\\\\") or path.startswith("//")


def home_dir(env=None) -> str:
    """The user's home directory, as the operating system knows it.

    Never $HOME or %USERPROFILE%: Claude Code hands hooks the env block of a
    project's committed .claude/settings.json, so a cloned repo could point
    them at a folder it ships, with its own key and path rules in it.

    `env` is read only when the test suite has installed its marker module
    (it never ships), so tests can give each subprocess its own home.
    """
    if _tests_trust_env_home():
        return _env_home(os.environ if env is None else env)
    return _windows_home() if IS_WINDOWS else _posix_home()


def _tests_trust_env_home() -> bool:
    """The suite's marker module, honoured only in a git checkout: an
    installed plugin (the marketplace cache) has no .git, so a marker that
    somehow got there still changes nothing."""
    import importlib.util
    try:
        if importlib.util.find_spec("rius_cc._tests_trust_env_home") is None:
            return False
    except (ImportError, ValueError):
        return False
    return os.path.exists(os.path.join(_plugin_root(), ".git"))


def _plugin_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))


def _posix_home() -> str:
    import pwd
    return pwd.getpwuid(os.getuid()).pw_dir


# FOLDERID_Profile
_PROFILE_FOLDER_ID = "{5E6C858F-0E22-4760-9AFE-EA3317B67173}"


def _windows_home(shell32=None, ole32=None) -> str:
    """The profile folder from SHGetKnownFolderPath, which reads the
    registry, not the environment."""
    import ctypes
    from ctypes import wintypes
    import uuid

    class GUID(ctypes.Structure):
        _fields_ = [("data", ctypes.c_ubyte * 16)]

    shell32 = shell32 or ctypes.WinDLL("shell32")
    ole32 = ole32 or ctypes.WinDLL("ole32")
    folder = GUID()
    folder.data[:] = uuid.UUID(_PROFILE_FOLDER_ID).bytes_le
    path = wintypes.LPWSTR()
    result = shell32.SHGetKnownFolderPath(ctypes.byref(folder), 0, None,
                                          ctypes.byref(path))
    try:
        if result != 0 or not path.value:
            raise OSError("SHGetKnownFolderPath failed: %r" % result)
        return path.value
    finally:
        ole32.CoTaskMemFree(path)


def _env_home(env) -> str:
    """The test suite's home: $HOME when it is a native path, else
    %USERPROFILE% (Git Bash hands Python an MSYS $HOME)."""
    if not IS_WINDOWS:
        return env.get("HOME") or os.path.expanduser("~")
    for name in ("HOME", "USERPROFILE"):
        value = env.get(name)
        if value and is_native_absolute(value):
            return value
    return os.path.expanduser("~")


# ---------------------------------------------------------------------------
# Private files
# ---------------------------------------------------------------------------
#
# Logs, state and overrides hold paths, session ids and prompt-derived
# titles, so everything the plugin keeps under ~/.claude/rius/ is owner-only.
# On Windows the mode bits are ignored and the files inherit the profile ACL.

PRIVATE_DIR_MODE = 0o700
PRIVATE_FILE_MODE = 0o600


def ensure_private_dir(path: str) -> str:
    """mkdir -p `path`, then make `path` itself owner-only. Tightening an
    existing directory too is what fixes installs made before this."""
    os.makedirs(path, mode=PRIVATE_DIR_MODE, exist_ok=True)
    if not IS_WINDOWS:
        try:
            if stat.S_IMODE(os.stat(path).st_mode) != PRIVATE_DIR_MODE:
                os.chmod(path, PRIVATE_DIR_MODE)
        except OSError:
            pass
    return path


def rius_dir(home: str, *parts: str) -> str:
    """The active agent's rius dir[/parts...], each level created owner-only."""
    path = ensure_private_dir(agent.active().rius_dir(home))
    for part in parts:
        path = ensure_private_dir(os.path.join(path, part))
    return path


def open_private_append(path: str):
    """open(path, "a"), but a file it creates is 0600, not 0644."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0)
    return os.fdopen(os.open(path, flags, PRIVATE_FILE_MODE), "a")


# ---------------------------------------------------------------------------
# Breadcrumbs from the seam itself
# ---------------------------------------------------------------------------

_WARNED = set()


def _warn_once(key: str, message: str) -> None:
    """Append one line to the debug log, at most once per `key` per process.

    ``rius_cc.log`` cannot be used here: it imports ``config``, which imports
    this module. So this writes the same file directly.

    At most once because the callers are polled -- the heartbeat probes
    liveness every 15 seconds for the life of the session, and a broken
    probe would otherwise write a line each time. Unconditional (not gated
    on ``cfg.debug``) for the same reason ``log.write(force=True)`` exists:
    the failures routed here are ones that would otherwise be invisible.
    """
    if key in _WARNED:
        return
    _WARNED.add(key)
    try:
        import datetime

        now = datetime.datetime.now(datetime.timezone.utc)
        d = rius_dir(home_dir(), "log")
        path = os.path.join(d, now.strftime("%Y-%m-%d") + ".log")
        with open_private_append(path) as fh:
            fh.write("%s platform_compat: %s\n"
                     % (now.strftime("%Y-%m-%dT%H:%M:%SZ"), message))
    except BaseException:
        # A logger that can break the session it observes is worse than no
        # logger. Same rule as rius_cc.log.
        pass


# ---------------------------------------------------------------------------
# Process liveness  (TRAP 1)
# ---------------------------------------------------------------------------

def _kernel32():  # pragma: no cover - Windows only
    """kernel32 with restypes set.

    ctypes defaults every return to C ``int``. A 64-bit HANDLE truncated to
    32 bits is a handle that neither closes nor queries, so the argtypes
    below are load-bearing, not decoration.
    """
    import ctypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = ctypes.c_void_p
    k32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    k32.GetExitCodeProcess.restype = ctypes.c_int
    k32.GetExitCodeProcess.argtypes = [ctypes.c_void_p,
                                       ctypes.POINTER(ctypes.c_ulong)]
    k32.CloseHandle.restype = ctypes.c_int
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    return k32


def _posix_pid_alive(pid: int, kill=None) -> bool:
    """Signal 0 on POSIX is a pure permission/existence probe."""
    kill = os.kill if kill is None else kill
    try:
        kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just owned by someone else
    except OSError:
        return False
    return True


def _windows_pid_alive(pid: int, kernel32=None, last_error=None) -> bool:
    """Liveness WITHOUT os.kill -- see the module note and the report.

    ``os.kill(pid, 0)`` is not a probe on Windows. CPython's ``os_kill_impl``
    (Modules/posixmodule.c) branches on the signal value: ``CTRL_C_EVENT``
    is 0 in the Windows SDK, so signal 0 takes the FIRST branch and calls
    ``GenerateConsoleCtrlEvent(CTRL_C_EVENT, pid)`` -- a Ctrl+C delivered to
    the console process group with that id, i.e. to Claude Code itself. On a
    build without console IO the same call falls through to
    ``OpenProcess(PROCESS_ALL_ACCESS)`` + ``TerminateProcess(handle, 0)``,
    which the os.kill docs describe as "unconditionally killed". Either way
    the heartbeat's liveness check would attack the process it is watching.

    OpenProcess + GetExitCodeProcess only reads. PROCESS_QUERY_LIMITED_-
    INFORMATION is the narrowest mask that answers the question and, unlike
    PROCESS_ALL_ACCESS, is granted across integrity levels.

    ``_posix_pid_alive`` is total by construction -- every way ``os.kill``
    can fail is an ``OSError``. This one is not: ``ctypes.WinDLL`` raises
    ``OSError``, ``ctypes.ArgumentError`` is NOT an ``OSError`` subclass,
    and either would propagate out of ``Pinger.run()`` into heartbeat.py's
    module-tail ``except BaseException: pass`` -- killing the pinger
    mid-session with no stopped ping and no log. So the whole body is
    wrapped, and an unreadable answer means ALIVE: calling a healthy
    session dead stops the heartbeat, which is the outage; calling a dead
    one alive costs at most one interval and the backend's stale path
    already handles it.
    """
    try:
        return _windows_pid_alive_inner(pid, kernel32, last_error)
    except BaseException as exc:      # noqa: BLE001 - see the docstring
        _warn_once("pid_alive",
                   "pid_alive probe failed (%s: %s); assuming the watched "
                   "process is alive" % (type(exc).__name__, exc))
        return True


def _windows_pid_alive_inner(pid: int, kernel32=None, last_error=None) -> bool:
    import ctypes

    if kernel32 is None:  # pragma: no cover - Windows only
        kernel32 = _kernel32()
    if last_error is None:
        # getattr, not a bare attribute: ctypes.get_last_error is Windows-only,
        # and this branch is unit-tested with fakes on POSIX.
        last_error = getattr(ctypes, "get_last_error", None) or (lambda: 0)

    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0,
                                  int(pid))
    if not handle:
        # ACCESS_DENIED means the process is there and we may not look --
        # the same case POSIX reports as PermissionError, and the same
        # answer: alive. Anything else (typically ERROR_INVALID_PARAMETER)
        # means no such process.
        return last_error() == ERROR_ACCESS_DENIED
    try:
        code = ctypes.c_ulong(0)
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            # The handle opened, so the process object exists. Reporting it
            # dead here would stop the heartbeat on a live session.
            return True
        return code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def pid_alive(pid: int) -> bool:
    """Is `pid` a live process? Never signals, terminates or interrupts it."""
    if pid is None or int(pid) <= 0:
        # 0 is "no process to watch" (see parent_pid_of) and on POSIX would
        # mean the caller's whole process group. Never probe it.
        return False
    if IS_WINDOWS:
        return _windows_pid_alive(int(pid))
    return _posix_pid_alive(int(pid))


# ---------------------------------------------------------------------------
# Parent pid lookup  (TRAP 5)
# ---------------------------------------------------------------------------

def _posix_parent_pid_of(pid: int, check_output=None) -> int:
    check_output = subprocess.check_output if check_output is None else check_output
    try:
        out = check_output(["ps", "-o", "ppid=", "-p", str(pid)],
                           stderr=subprocess.DEVNULL, timeout=PS_TIMEOUT_S)
        parent = int(out.decode().strip())
    except BaseException:
        return 0
    return parent if parent > 0 else 0


def _toolhelp_pairs():  # pragma: no cover - Windows only
    """[(pid, ppid), ...] from a Toolhelp snapshot.

    `ps` does not exist on Windows and `wmic` was removed in Windows 11
    24H2, so this is ctypes against kernel32 directly. PowerShell would
    work but costs a second of startup in a hook that runs in the session's
    critical path.
    """
    import ctypes

    class PROCESSENTRY32(ctypes.Structure):
        _fields_ = [
            ("dwSize", ctypes.c_ulong),
            ("cntUsage", ctypes.c_ulong),
            ("th32ProcessID", ctypes.c_ulong),
            ("th32DefaultHeapID", ctypes.c_size_t),   # ULONG_PTR
            ("th32ModuleID", ctypes.c_ulong),
            ("cntThreads", ctypes.c_ulong),
            ("th32ParentProcessID", ctypes.c_ulong),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", ctypes.c_ulong),
            ("szExeFile", ctypes.c_char * 260),
        ]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    k32.CreateToolhelp32Snapshot.argtypes = [ctypes.c_ulong, ctypes.c_ulong]
    k32.Process32First.restype = ctypes.c_int
    k32.Process32First.argtypes = [ctypes.c_void_p,
                                   ctypes.POINTER(PROCESSENTRY32)]
    k32.Process32Next.restype = ctypes.c_int
    k32.Process32Next.argtypes = [ctypes.c_void_p,
                                  ctypes.POINTER(PROCESSENTRY32)]
    k32.CloseHandle.restype = ctypes.c_int
    k32.CloseHandle.argtypes = [ctypes.c_void_p]

    snapshot = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snapshot or snapshot == ctypes.c_void_p(-1).value:
        return []
    pairs = []
    try:
        entry = PROCESSENTRY32()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
        ok = k32.Process32First(snapshot, ctypes.byref(entry))
        while ok and len(pairs) < MAX_PROCESSES:
            pairs.append((int(entry.th32ProcessID),
                          int(entry.th32ParentProcessID)))
            ok = k32.Process32Next(snapshot, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snapshot)
    return pairs


def _windows_parent_pid_of(pid: int, pairs=None) -> int:
    """`pairs` is the (pid, ppid) snapshot; injectable so this is testable."""
    try:
        rows = _toolhelp_pairs() if pairs is None else pairs()
    except BaseException:
        return 0
    for row_pid, row_ppid in rows:
        if row_pid == pid:
            # A parent of 0 (or of itself) is not usable as a watch target.
            return row_ppid if row_ppid > 0 and row_ppid != pid else 0
    return 0


def parent_pid_of(pid: int) -> int:
    """Parent pid of `pid`, or 0 when it cannot be determined.

    0 means "unknown", never "init" and never a fallback to some other pid.
    Callers must treat it as "do not watch anything" -- see hook.py. Guessing
    here is how a recycled pid becomes somebody else's parent.
    """
    if not pid or pid <= 0:
        return 0
    if IS_WINDOWS:
        return _windows_parent_pid_of(int(pid))
    return _posix_parent_pid_of(int(pid))


# ---------------------------------------------------------------------------
# File locking  (TRAP 2)
# ---------------------------------------------------------------------------

def open_lock_file(path: str, mode: int = PRIVATE_FILE_MODE) -> int:
    """A fresh descriptor for the lock file. NEVER cached per path.

    Both implementations rely on one-lock-per-descriptor: POSIX flock is
    per open file description, and Windows byte-range locks are per HANDLE.
    Reusing a descriptor would make a process invisible to its own lock.
    """
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_BINARY", 0)
    return os.open(path, flags, mode)


def _posix_try_lock(fd: int, fcntl_mod=None) -> bool:
    if fcntl_mod is None:
        import fcntl as fcntl_mod  # noqa: F811  (POSIX only, imported lazily)
    try:
        fcntl_mod.flock(fd, fcntl_mod.LOCK_EX | fcntl_mod.LOCK_NB)
    except OSError:
        return False
    return True


def _windows_try_lock(fd: int, msvcrt_mod=None) -> bool:
    """One byte at offset 0, non-blocking.

    msvcrt.locking locks `nbytes` from the CURRENT file position, so the
    seek is part of the contract -- without it two callers could lock two
    different bytes and both believe they won. Locking a byte past EOF is
    explicitly allowed by LockFile, so the empty lock file is fine.

    LK_NBLCK and not LK_LOCK: LK_LOCK has its own 10-retries-at-1-second
    behaviour that the caller cannot control, which is the opposite of the
    bounded, caller-supplied deadline state.session_lock promises.
    """
    try:
        if msvcrt_mod is None:  # pragma: no cover - Windows only
            import msvcrt as msvcrt_mod  # noqa: F811
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt_mod.locking(fd, msvcrt_mod.LK_NBLCK, 1)
    except OSError:
        return False                  # contended: the expected failure
    except BaseException as exc:      # noqa: BLE001
        # Not an OSError (a bad descriptor reaches ctypes.ArgumentError, a
        # missing msvcrt an ImportError). "Could not take the lock" is the
        # honest answer and every caller already handles it; raising here
        # would abort a hook instead.
        _warn_once("try_lock", "lock attempt failed (%s: %s); treating the "
                               "lock as contended" % (type(exc).__name__, exc))
        return False
    return True


def try_lock(fd: int) -> bool:
    """Try once for an exclusive lock. True if won, False if contended."""
    if IS_WINDOWS:
        return _windows_try_lock(fd)
    return _posix_try_lock(fd)


def _windows_unlock(fd: int, msvcrt_mod=None) -> None:
    try:
        if msvcrt_mod is None:  # pragma: no cover - Windows only
            import msvcrt as msvcrt_mod  # noqa: F811
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt_mod.locking(fd, msvcrt_mod.LK_UNLCK, 1)
    except OSError:
        pass
    except BaseException as exc:      # noqa: BLE001
        # unlock() promises never to raise, and closing the descriptor
        # releases the lock anyway -- so there is nothing to escalate.
        _warn_once("unlock", "unlock failed (%s: %s)"
                             % (type(exc).__name__, exc))


def unlock(fd: int) -> None:
    """Release a lock taken with try_lock. Best effort, never raises.

    Closing the descriptor releases the lock on both platforms; Windows
    documents the explicit unlock as the supported way, so do it there.
    """
    if IS_WINDOWS:
        _windows_unlock(fd)


# ---------------------------------------------------------------------------
# Detached spawn  (TRAP 3)
# ---------------------------------------------------------------------------

def detached_child_kwargs() -> dict:
    """Popen kwargs for a child that must outlive the hook process.

    ``start_new_session`` is POSIX-only (setsid); on Windows the equivalent
    is DETACHED_PROCESS, which gives the child no console to be killed with,
    plus CREATE_NEW_PROCESS_GROUP so a Ctrl+C in Claude Code's console is
    not delivered to our exporter.
    """
    if IS_WINDOWS:
        flags = (getattr(subprocess, "DETACHED_PROCESS", DETACHED_PROCESS) |
                 getattr(subprocess, "CREATE_NEW_PROCESS_GROUP",
                         CREATE_NEW_PROCESS_GROUP))
        return {"creationflags": flags}
    return {"start_new_session": True}


# ---------------------------------------------------------------------------
# Atomic replace
# ---------------------------------------------------------------------------

def replace_atomic(src: str, dst: str, attempts: int = REPLACE_ATTEMPTS,
                   delay: float = REPLACE_DELAY_S, sleep=time.sleep,
                   replace=None) -> None:
    """os.replace, retried briefly on Windows.

    os.replace DOES overwrite an existing destination on Windows -- it is
    MoveFileEx with MOVEFILE_REPLACE_EXISTING -- so the temp-file-then-
    rename save stays atomic. What differs is sharing: the rename fails
    while any other process has the destination open, and Python's open()
    does not ask for FILE_SHARE_DELETE. `/rius:status` and a concurrent
    exporter both read state.json, so a save can lose a race it would
    always win on POSIX. Retry briefly, then raise as before.
    """
    replace = os.replace if replace is None else replace
    if not IS_WINDOWS:
        replace(src, dst)
        return
    for attempt in range(max(1, attempts)):
        try:
            replace(src, dst)
            return
        except OSError:
            if attempt == max(1, attempts) - 1:
                raise
            sleep(delay)
