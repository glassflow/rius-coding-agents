# The user's home folder as the operating system knows it, for the shell
# launchers. SOURCED, never executed; the caller sets $dir to scripts/.
#
# Never $HOME or $USERPROFILE: Claude Code hands hooks the env block of a
# project's committed .claude/settings.json, so a cloned repo could point
# them at a folder it ships (platform_compat.home_dir says the same).
#
# Sets:
#   rius_os_home      the home folder, empty when it could not be resolved
#   rius_trusts_env   1 only under the test suite, which gives each test its
#                     own $HOME through a marker module that never ships,
#                     and only in a git checkout
#
# POSIX: `~name` expands from the passwd database (getpwnam), which also
# covers directory services on macOS, at the cost of one `id`. Git Bash:
# the Windows profile folder (CSIDL_PROFILE), which comes from the registry,
# not the environment.

rius_os_home=
rius_trusts_env=0
if [ -n "${dir:-}" ] && [ -e "$dir/rius_cc/_tests_trust_env_home.py" ] \
        && [ -e "$dir/../.git" ]; then
    rius_trusts_env=1
    rius_os_home=${HOME:-${USERPROFILE:-}}
else
    case "$(uname -s 2>/dev/null)" in
        MINGW*|MSYS*|CYGWIN*)
            rius_os_home=$(cygpath -u -F 40 2>/dev/null) || rius_os_home= ;;
        *)
            rius_user=$(id -un 2>/dev/null) || rius_user=
            case "$rius_user" in
                ""|-*|*[!A-Za-z0-9._-]*) ;;
                *) eval "rius_os_home=~$rius_user" ;;
            esac ;;
    esac
    case "$rius_os_home" in /*) ;; *) rius_os_home= ;; esac
fi
rius_os_home_resolved=1
