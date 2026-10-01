"""What differs about running the suite on Windows rather than POSIX.

The suite runs on both (see the `windows` job in ci.yml). Tests that probe
a POSIX-only behaviour skip themselves on Windows with `posix_only`, and
say why; tests that only make sense against a real Windows use
`windows_only`. Everything that launches a shell script goes through
`BASH`/`SH`, never a hard-coded /bin path.
"""
import os
import pathlib
import shutil
import sys

import pytest

IS_WINDOWS = sys.platform.startswith("win")


def posix_only(reason):
    return pytest.mark.skipif(IS_WINDOWS, reason="POSIX only: " + reason)


def windows_only(reason):
    return pytest.mark.skipif(not IS_WINDOWS, reason="Windows only: " + reason)


def _git_for_windows_bin():
    r"""Git for Windows' own bin\ directory, the bash Claude Code runs hooks in.

    Not `shutil.which("bash")`: CreateProcess searches System32 before PATH,
    and System32\bash.exe is the WSL launcher, which is not Git Bash and on
    a machine with no distro installed runs nothing at all.
    """
    git = shutil.which("git")
    if not git:
        return None
    root = pathlib.Path(git).resolve().parent.parent   # ...\Git\cmd\git.exe
    return root / "bin" if (root / "bin" / "bash.exe").exists() else None


if IS_WINDOWS:
    _bin = _git_for_windows_bin()
    BASH = str(_bin / "bash.exe") if _bin else "bash"
    SH = str(_bin / "sh.exe") if _bin else "sh"
    # Coreutils (mkdir, date, cat) and no Python: Git's MSYS userland.
    TOOLS_WITHOUT_PYTHON = str(_bin.parent / "usr" / "bin") if _bin else ""
else:
    BASH = "/bin/bash"
    SH = "/bin/sh"
    TOOLS_WITHOUT_PYTHON = os.pathsep.join(("/usr/bin", "/bin"))

# A shell-script stub that execs the interpreter running the suite. MSYS
# accepts forward-slashed Windows paths, and a backslash is one less thing
# to reason about inside double quotes.
PYTHON_FOR_SH = sys.executable.replace("\\", "/")


def minimal_env(**overrides):
    """A hand-built subprocess environment that still lets Python start.

    Windows Python cannot open a socket without SYSTEMROOT (WinError 10106),
    so an env of just HOME and PATH yields an exporter that silently never
    POSTs. Claude Code hands hooks its full environment, so carrying these
    through is what a real hook sees.
    """
    env = {}
    if IS_WINDOWS:
        for name in ("SYSTEMROOT", "SystemRoot", "WINDIR", "COMSPEC", "TEMP",
                     "TMP", "USERPROFILE", "LOCALAPPDATA", "APPDATA"):
            if name in os.environ:
                env[name] = os.environ[name]
    env.update(overrides)
    return env
