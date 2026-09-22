#!/bin/sh
# Find a Python and hand it hook.py. Nothing else.
#
# hooks.json used to run hook.py directly and rely on its
# `#!/usr/bin/env python3` shebang. Windows has no shebang support, and the
# interpreter there may be `py`, `python` or `python3` depending on how it
# was installed -- so on Windows the hook simply did not run, and because
# hooks are required to exit 0 and write nothing to stdout, nothing said so.
#
# Rules this script inherits from hook.py and must not break:
#   * never write to stdout -- stdout is a control channel for hooks;
#   * never exit non-zero -- Claude Code treats that as a hook failure;
#   * pass stdin through untouched -- the hook payload arrives on it.
#
# When no interpreter can be found there is nothing to run, so the one thing
# left is to say why somewhere a human can find it. Silence is the failure
# mode this whole plugin exists to avoid.

# Parameter expansion, not `dirname`: this must still work when PATH is
# broken enough that no external command resolves. Both separators, because
# $0 can arrive as C:\...\scripts\hook.sh on a native Windows shell.
#
# No `dir=.` fallback. A $0 with no separator at all means this script does
# not know where its own directory is, and "." is a guess that runs whatever
# ./hook.py happens to be -- or nothing -- without saying either. Fall
# through to the breadcrumb instead.
case "$0" in
    */*)  dir=${0%/*} ;;
    *\\*) dir=${0%\\*} ;;
    *)    dir= ;;
esac

# Guarded, not bare: a failed `.` would end this shell non-zero, which is
# the one thing a hook may never do.
if [ -n "$dir" ] && [ -r "$dir/_find_python.sh" ]; then
    . "$dir/_find_python.sh"
elif [ -n "$dir" ]; then
    # _find_python.sh is missing or unreadable: this is a packaging fault,
    # not the PATH problem the fallback message below otherwise implies.
    # Say so distinctly, or the breadcrumb blames PATH with an empty
    # candidate list as the only tell.
    rius_candidates="<scripts/_find_python.sh missing or unreadable>"
fi

if [ -n "$dir" ] && [ -n "${rius_py:-}" ]; then
    # `exec` is load-bearing, not a micro-optimisation. Because this shell
    # is REPLACED, hook.py keeps the launcher's pid, so hook.py's parent is
    # still the shell Claude Code spawned and `_claude_code_pid()`'s
    # one-level walk up still lands on Claude Code. Drop the exec and that
    # walk lands one level short, on a shell that is already exiting: the
    # pinger would see a dead process on its first iteration and quit,
    # producing zero heartbeats -- silently, as ever.
    exec "$rius_py" "$dir/hook.py" "$@"
fi

# Nothing to run. Leave a breadcrumb, then exit 0 like every other path here.
# ${HOME:-$USERPROFILE} deliberately duplicates, rather than calls,
# `platform_compat.home_dir` -- there is no Python here to ask. The two can
# disagree on a non-standard Git Bash install (where $HOME is an MSYS path
# like /c/Users/me that home_dir rejects as non-native), so this file may
# land under a different root than the plugin's own logs. It is a last-
# resort message written when nothing else can run; a second location for
# it is a far smaller problem than no message at all.
log_dir="${HOME:-$USERPROFILE}/.claude/rius/log"
if [ -n "$dir" ]; then
    reason="no Python interpreter found on PATH (tried: $rius_candidates).
Install Python 3.9+ and make sure one of those names is on PATH."
else
    reason="cannot locate its own directory: \$0 was \"$0\", which names no
directory. Claude Code should invoke it by path via \${CLAUDE_PLUGIN_ROOT}."
fi
mkdir -p "$log_dir" 2>/dev/null && cat >>"$log_dir/bootstrap.log" 2>/dev/null <<EOF
$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null) rius hook "$1": $reason
The Rius plugin cannot run and is tracing nothing.
EOF
exit 0
