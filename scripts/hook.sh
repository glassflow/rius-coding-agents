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
# broken enough that no external command resolves.
case "$0" in
    */*) dir=${0%/*} ;;
    *)   dir=. ;;
esac

# On Windows, `py` (the PEP 397 launcher) is tried first and deliberately.
# `python3` there is usually the Microsoft Store's App Execution Alias: a
# stub that `command -v` finds, that is not Python, and that opens the Store
# when run. `py` is a real binary or absent.
if [ "$OS" = "Windows_NT" ]; then
    candidates="py python python3"
    probe=1
else
    candidates="python3 python"
    probe=0
fi

for py in $candidates; do
    command -v "$py" >/dev/null 2>&1 || continue
    # Only paid on Windows, and only to rule out the Store stub described
    # above -- a POSIX `python3` on PATH is the same thing the shebang
    # resolved to, so it is taken at its word.
    if [ "$probe" = "1" ]; then
        "$py" -c "" >/dev/null 2>&1 || continue
    fi
    exec "$py" "$dir/hook.py" "$@"
done

# No interpreter. Leave a breadcrumb, then exit 0 like every other path here.
log_dir="${HOME:-$USERPROFILE}/.claude/rius/log"
mkdir -p "$log_dir" 2>/dev/null && cat >>"$log_dir/bootstrap.log" 2>/dev/null <<EOF
$(date -u +%Y-%m-%dT%H:%M:%SZ) rius hook "$1": no Python interpreter found on
PATH (tried: $candidates). The Rius plugin cannot run and is tracing nothing.
Install Python 3.9+ and make sure one of those names is on PATH.
EOF
exit 0
