#!/bin/sh
# Find a Python and hand it rius_ctl.py. The launcher for the /rius:* commands.
#
# The command files used to run scripts/rius_ctl.py directly and rely on its
# `#!/usr/bin/env python3` shebang -- the same assumption that stopped
# hook.py from ever running on Windows. It matters more here: `/rius:status`
# is the ONLY thing that distinguishes a correctly-installed-but-disabled
# plugin from a broken one, so on Windows the command that would have
# explained the silence was itself silent.
#
# Unlike hook.sh, this one is REQUIRED to write to stdout: its stdout is the
# slash command's output. So when there is no interpreter it says so there,
# where the user is already looking, instead of in a log file. It still
# exits 0 -- a failed slash command tells the user less than a sentence
# does.

# See hook.sh for why this is parameter expansion, why both separators, and
# why there is no `.` fallback.
case "$0" in
    */*)  dir=${0%/*} ;;
    *\\*) dir=${0%\\*} ;;
    *)    dir= ;;
esac

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
    exec "$rius_py" "$dir/rius_ctl.py" "$@"
fi

if [ -n "$dir" ]; then
    echo "Rius: no Python interpreter found on PATH (tried: $rius_candidates)."
    echo "The plugin is installed but cannot run, so nothing is being traced."
    echo "Install Python 3.9+ and make sure one of those names is on PATH."
else
    echo "Rius: cannot locate the plugin's scripts directory from \"$0\"."
fi
exit 0
