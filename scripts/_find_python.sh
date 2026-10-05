# Interpreter resolution, shared by the two things Claude Code invokes:
# scripts/hook.sh (every hook event) and scripts/rius_ctl.sh (/rius:*).
#
# SOURCED, never executed. The caller needs the winning interpreter in its
# OWN shell so it can `exec` it -- see the note on the exec in hook.sh.
#
# Sets (plus rius_-prefixed scratch variables and helpers):
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
rius_macos=0
case "${OS:-}" in Windows_NT) rius_windows=1 ;; esac
[ -n "${WINDIR:-}" ] && rius_windows=1
[ -n "${windir:-}" ] && rius_windows=1
case "$(uname -s 2>/dev/null)" in
    MINGW*|MSYS*|CYGWIN*) rius_windows=1 ;;
    Darwin) rius_macos=1 ;;
esac

# On a Mac without the Command Line Tools, /usr/bin/python3 is a stub that
# opens the "install developer tools" dialog when run -- so the probe below
# would pop it on every hook. `xcode-select -p` fails quietly in that case.
rius_is_macos_stub() {
    [ "$rius_macos" = "1" ] || return 1
    case "$1" in /usr/bin/python3|/usr/bin/python) ;; *) return 1 ;; esac
    ! xcode-select -p >/dev/null 2>&1 </dev/null
}

# On Windows `py` (the PEP 397 launcher) is tried first and deliberately.
# `python3` there is usually the Microsoft Store's App Execution Alias: a
# stub that `command -v` finds, that is not Python, and that opens the Store
# when run. `py` is a real binary or absent.
if [ "$rius_windows" = "1" ]; then
    rius_candidates="py python python3"
else
    rius_candidates="python3 python"
fi

# A repository can set PATH for every hook through its .claude/settings.json,
# so an interpreter that lives in the project folder (or a PATH entry that is
# relative, and so means "somewhere in the project") is never run. The
# project folder is Claude Code's CLAUDE_PROJECT_DIR (Cursor's
# CURSOR_PROJECT_DIR), else the cwd, which is Codex's session folder. When that
# is the home folder or /, everything would be "inside", so the rule is off.
rius_home=${HOME:-${USERPROFILE:-}}
rius_project=$(cd -P -- "${CLAUDE_PROJECT_DIR:-${CURSOR_PROJECT_DIR:-$PWD}}" \
    2>/dev/null && pwd -P)
rius_home_real=$(cd -P -- "${rius_home:-/}" 2>/dev/null && pwd -P)
case "$rius_project" in /|"$rius_home_real") rius_project= ;; esac

rius_real_path() {
    realpath -- "$1" 2>/dev/null || readlink -f -- "$1" 2>/dev/null \
        || printf '%s\n' "$1"
}

rius_in_project() {
    [ -n "$rius_project" ] || return 1
    rius_checked=$1
    # Git Bash spells the project /c/...; a pin may say C:/...
    case "$rius_checked" in
        [A-Za-z]:/*) rius_checked=$(cygpath -u "$rius_checked" 2>/dev/null) \
                         || rius_checked=$1 ;;
    esac
    case "$rius_checked" in "$rius_project"|"$rius_project"/*) return 0 ;; esac
    return 1
}

rius_is_absolute() {
    case "$1" in /*|[A-Za-z]:[\\/]*) return 0 ;; esac
    return 1
}

rius_outside_project() {
    rius_is_absolute "$1" && [ -x "$1" ] || return 1
    ! rius_in_project "$1" && ! rius_in_project "$(rius_real_path "$1")"
}

rius_runs_python() {
    rius_is_macos_stub "$1" && return 1
    # Unconditional, on every platform: it makes "is this actually a
    # Python?" independent of the platform guess above -- the Store stub
    # fails here, and so does anything else on PATH that merely shares the
    # name. `< /dev/null` because the candidate is by definition not trusted
    # to be Python: one that reads stdin would drain the hook payload.
    "$1" -I -c "" >/dev/null 2>&1 </dev/null
}

# What `command -v name` would find, except that PATH entries in the project
# are passed over. As with `command -v`, the first match is the answer.
rius_search_path() {
    rius_rest=${PATH:-}:
    while [ -n "$rius_rest" ]; do
        rius_entry=${rius_rest%%:*}
        rius_rest=${rius_rest#*:}
        [ -n "$rius_entry" ] || continue
        for rius_file in "$rius_entry/$1" "$rius_entry/$1.exe"; do
            [ -f "$rius_file" ] && rius_outside_project "$rius_file" || continue
            rius_runs_python "$rius_file" || return 1
            printf '%s\n' "$rius_file"
            return 0
        done
    done
    return 1
}

# An interpreter the user pinned: one absolute path on the first line of
# ~/.claude/rius/python. Tried before PATH, under the same rules.
rius_pin_file="$rius_home/.claude/rius/python"
rius_pinned=
if [ -n "$rius_home" ] && [ -r "$rius_pin_file" ]; then
    { IFS= read -r rius_pinned <"$rius_pin_file"; } 2>/dev/null
    # A file saved on Windows ends its line in CR and may use backslashes.
    rius_pinned=$(printf '%s' "$rius_pinned" | tr -d '\r' | tr '\\' /)
fi

rius_py=
if [ -n "$rius_pinned" ] && rius_outside_project "$rius_pinned" \
        && rius_runs_python "$rius_pinned"; then
    rius_py=$rius_pinned
else
    for rius_candidate in $rius_candidates; do
        rius_py=$(rius_search_path "$rius_candidate") && break
        rius_py=
    done
fi
