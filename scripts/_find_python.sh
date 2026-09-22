# Interpreter resolution, shared by the two things Claude Code invokes:
# scripts/hook.sh (every hook event) and scripts/rius_ctl.sh (/rius).
#
# SOURCED, never executed. The caller needs the winning interpreter in its
# OWN shell so it can `exec` it -- see the note on the exec in hook.sh.
#
# Sets, and nothing else:
#   rius_py          the interpreter to run, empty if none of them works
#   rius_candidates  what was tried, for the caller's message
#
# Constraints inherited from hook.sh: never write to stdout (it is a hook
# control channel), never exit non-zero, and leave the caller's stdin
# untouched -- the hook payload is on it.

# Windows detection only REORDERS the candidates; the probe below is what
# actually decides. $OS was once the sole gate, and nothing guarantees
# Claude Code hands a hook shell a full environment: a missing $OS on
# Windows took the POSIX branch, put the Store alias first, and skipped the
# probe that would have rejected it. So: several signals, none load-bearing.
rius_windows=0
case "${OS:-}" in Windows_NT) rius_windows=1 ;; esac
[ -n "${WINDIR:-}" ] && rius_windows=1
[ -n "${windir:-}" ] && rius_windows=1
case "$(uname -s 2>/dev/null)" in
    MINGW*|MSYS*|CYGWIN*) rius_windows=1 ;;
esac

# On Windows `py` (the PEP 397 launcher) is tried first and deliberately.
# `python3` there is usually the Microsoft Store's App Execution Alias: a
# stub that `command -v` finds, that is not Python, and that opens the Store
# when run. `py` is a real binary or absent.
if [ "$rius_windows" = "1" ]; then
    rius_candidates="py python python3"
else
    rius_candidates="python3 python"
fi

rius_py=
for rius_candidate in $rius_candidates; do
    command -v "$rius_candidate" >/dev/null 2>&1 || continue
    # Unconditional, on every platform. It costs one process in a path that
    # is about to spawn a Python anyway, and it makes "is this actually a
    # Python?" independent of the platform guess above -- which is the
    # point: the Store stub fails here, and so does anything else on PATH
    # that merely shares the name.
    #
    # `< /dev/null` because the candidate is by definition not trusted to
    # be Python: without it, a candidate that reads stdin drains the hook
    # payload and the real interpreter gets an empty one.
    "$rius_candidate" -c "" >/dev/null 2>&1 </dev/null || continue
    rius_py=$rius_candidate
    break
done
