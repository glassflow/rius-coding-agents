# Windows support for `rius-claude-code`

Branch `feat/windows-support`. 196 tests before, 258 after; all pass on
Python 3.12 and on the 3.9 runtime floor. `.github/workflows/ci.yml` was
**not** modified (scope change mid-task; the `windows-latest` job is a
separate follow-up). That makes the new unit tests the *entire* automated
safety net for Windows — see [Residual risk](#residual-risk).

## The shape of the fix

All platform differences now live in `scripts/rius_cc/platform_compat.py`,
in a three-part pattern per difference:

* `_posix_x(...)` — runs natively in this suite,
* `_windows_x(...)` — OS dependencies are arguments with lazy defaults, so
  the branch is driven with fakes on macOS/Linux,
* `x(...)` — a dispatcher on `IS_WINDOWS` and nothing else.

Windows-only modules (`msvcrt`, `ctypes`) are imported **inside** the
functions that need them. Importing `msvcrt` at module scope would break
POSIX in exactly the way `fcntl` broke Windows.

`/rius status` now prints a `Platform:` line naming the live implementation,
so a Windows user whose plugin is doing nothing has something to look at.

---

## TRAP 1 — `os.kill(pid, 0)` in the heartbeat

**Verified. The claim is right about the danger and wrong about the
mechanism, and the real mechanism is arguably worse.**

I pulled CPython 3.12's `Modules/posixmodule.c` and read `os_kill_impl`
(line 8964). The Windows branch is:

```c
#ifdef HAVE_WINDOWS_CONSOLE_IO
    if (sig == CTRL_C_EVENT || sig == CTRL_BREAK_EVENT) {
        if (GenerateConsoleCtrlEvent(sig, (DWORD)pid) == 0) { ... }
        Py_RETURN_NONE;
    }
#endif
    HANDLE handle = OpenProcess(PROCESS_ALL_ACCESS, FALSE, (DWORD)pid);
    ...
    BOOL res = TerminateProcess(handle, sig);
```

The brief said signal 0 reaches `TerminateProcess`. It does not, on a normal
build — because **`CTRL_C_EVENT` is `0`** in the Windows SDK (`wincon.h`),
so `sig == CTRL_C_EVENT` is true for `sig == 0` and the call takes the
*first* branch. `os.kill(pid, 0)` on Windows is
`GenerateConsoleCtrlEvent(CTRL_C_EVENT, pid)`: a **Ctrl+C delivered to the
console process group whose id is `pid`** — i.e. to Claude Code, whose
default handler terminates it.

Both outcomes are bugs, and which one you get depends on the build and the
pid:

| Situation | What actually happens |
|---|---|
| Normal desktop CPython, `pid` is a console process-group id | Ctrl+C to that whole group — Claude Code is interrupted/killed |
| Normal build, `pid` is not a group id | `ERROR_INVALID_PARAMETER` → `OSError` → old `_pid_alive` returns `False` → **pinger exits on its first iteration, zero heartbeats, silently** |
| Build without `HAVE_WINDOWS_CONSOLE_IO` | Falls through to `OpenProcess(PROCESS_ALL_ACCESS)` + `TerminateProcess(handle, 0)` — exactly the brief's claim |

The docs agree with the source (`Doc/library/os.rst`): "Any other value for
*sig* will cause the process to be unconditionally killed by the
TerminateProcess API".

**Done.** `platform_compat.pid_alive()` dispatches to
`_windows_pid_alive()`, which is `OpenProcess(PROCESS_QUERY_LIMITED_-
INFORMATION=0x1000, FALSE, pid)` → `GetExitCodeProcess` → `STILL_ACTIVE`
(259) means alive → `CloseHandle` in a `finally`.
`PROCESS_QUERY_LIMITED_INFORMATION` rather than `PROCESS_ALL_ACCESS`: it is
the narrowest mask that answers the question and is granted across integrity
levels. Failure semantics mirror POSIX: `ERROR_ACCESS_DENIED` → alive
(POSIX's `PermissionError`), anything else → dead. If the handle opens but
`GetExitCodeProcess` fails we answer *alive*, because the process object
demonstrably exists and guessing "dead" stops the heartbeat on a live
session. `pid_alive(0)` and negatives short-circuit to `False` without ever
probing — on POSIX `os.kill(0, 0)` addresses the caller's whole process
group.

`heartbeat.py` no longer references `os.kill` at all.

**Tested:**
- `test_windows_liveness_never_calls_os_kill` — patches `os.kill` to raise
  `AssertionError`, routes through the Windows dispatcher with a fake
  kernel32, asserts nothing raises.
- `test_liveness_never_uses_os_kill_on_windows` — the same guarantee at the
  real call site, driving a whole `Pinger.run()` to completion.
- Plus: read-only access mask asserted explicitly; handle always closed
  (three failure shapes); `STILL_ACTIVE`/exited/`ACCESS_DENIED`/
  `INVALID_PARAMETER`/unreadable-exit-code; `pid 0` never probed.

## TRAP 2 — file locking

**Done.** `state.py` no longer imports `fcntl` (or anything platform-
specific). `session_lock` now calls `platform_compat.open_lock_file()`,
`try_lock()` and `unlock()`.

* Windows uses `msvcrt.locking(fd, LK_NBLCK, 1)` — **`LK_NBLCK`, never
  `LK_LOCK`**, whose fixed 10-retries-at-1s is not a deadline we control.
  The bounded `block_timeout` wait stays where it was, in `session_lock`'s
  own retry loop.
* `os.lseek(fd, 0, SEEK_SET)` before every lock/unlock: `msvcrt.locking`
  locks from the *current* file position, so without the seek two callers
  could lock two different bytes and both believe they won. Locking a byte
  past EOF is explicitly permitted by `LockFile`, so the empty lock file is
  fine.
* The descriptor is still opened fresh per call and closed in `finally`,
  never cached per path. Windows byte-range locks belong to the **HANDLE**,
  so a cached fd would make a process invisible to its own lock. `unlock()`
  is only called when the lock was actually acquired.

`test_lock_is_exclusive` nests two locks in one process and expects the
inner to fail. That still holds on Windows: byte-range locks are enforced
between handles regardless of process, so the second `os.open` is refused.
Rather than assert that from the documentation, the fake models it — the
registry is keyed on the file's identity and its owner on the descriptor —
and **all seven of `session_lock`'s guarantees are re-run through the
Windows branch** in `tests/test_state.py`: exclusivity, release, per-session
independence, bounded-timeout give-up, acquire-on-release, non-blocking
default, and the `OSError`-not-masked property. Plus a `LK_LOCK` is never
used assertion, a seek-to-zero assertion, and a "lock does not outlive its
`with` block" assertion.

`os.replace` (used by `state.save`) **does** overwrite on Windows — it is
`MoveFileEx` with `MOVEFILE_REPLACE_EXISTING` — so the temp-file-then-rename
save stays atomic. What differs is sharing: the rename fails while another
process holds the destination open, and Python's `open()` does not pass
`FILE_SHARE_DELETE`. `/rius status` reads `state.json` without the lock, so
a save can lose a race it would always win on POSIX. Added
`platform_compat.replace_atomic()`: a plain `os.replace` on POSIX, and on
Windows a bounded retry (10 × 20ms) before raising as before. Tested both
ways.

## TRAP 3 — detached spawn

**Done.** `platform_compat.detached_child_kwargs()` returns
`{"start_new_session": True}` on POSIX and
`{"creationflags": DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP}` on Windows
(`0x8 | 0x200`, spelled out because `subprocess` only defines them on
Windows). `hook.py` splats it into both `Popen` calls. `CREATE_NEW_PROCESS_-
GROUP` matters beyond detachment: without it a Ctrl+C in Claude Code's
console would be delivered to our exporter.

**Tested:** `test_detached_spawn_kwargs_reach_popen_on_posix` and
`..._on_windows` capture the real `Popen` kwargs from a real `hook.main()`
run and assert `start_new_session` is present on one platform and *absent*
on the other. `tests/test_e2e.py` remains the POSIX proof that the child
genuinely outlives the parent; it is unchanged and passing.

## TRAP 4 — how the hook is invoked at all

**This one needed a decision, and the honest answer is that a single
`command` string cannot cover every configuration.** I checked Claude Code's
hooks reference rather than guessing. Relevant facts:

* There is a real `shell` field: `"bash"` or `"powershell"`. It "defaults to
  `bash`, or to `powershell` on Windows when Git Bash isn't installed".
* There is an exec form (`args`) that "spawns directly with no shell"; the
  docs bless `command: "node", args: [".../script.js"]` as the pattern that
  "works on every platform because `node.exe` is a real binary".

The exec form does not solve our problem, because there is no single Python
executable name that exists everywhere: POSIX has `python3`, Windows
installs give you `py` and/or `python`, and `python3` on Windows is usually
the Microsoft Store's App Execution Alias — a stub that `command -v` finds,
that is not Python, and that opens the Store when run.

**What I did:** kept the shell form with an explicit `"shell": "bash"`
(which the file already declared) and moved interpreter resolution into a
new `scripts/hook.sh`:

```json
"command": "bash \"${CLAUDE_PLUGIN_ROOT}/scripts/hook.sh\" SessionStart",
"shell": "bash"
```

`hook.sh` tries `py`, `python`, `python3` on Windows (`py` first,
deliberately — it is a real binary or absent, never a Store stub) and
`python3`, `python` on POSIX. On Windows only, it additionally runs
`"$py" -c ""` to rule out a stub; that probe is not paid on POSIX, where the
resolved `python3` is the same thing the shebang was already finding. It
derives its own directory with `${0%/*}` rather than `dinner`/`dirname`, so
it works even when `PATH` is broken. Invoking via `bash <script>` rather
than relying on the executable bit removes any question about how a Windows
git checkout records file modes.

If **no** interpreter is found it appends a line to
`~/.claude/rius/log/bootstrap.log` saying so, then exits 0. It never writes
to stdout and never exits non-zero.

**What this does not cover, stated plainly:** a Windows machine where Claude
Code falls back to PowerShell because Git Bash is not installed. Those hooks
will not run. Covering it needs a second hook entry with
`"shell": "powershell"`, and since Claude Code runs *every* entry for an
event, that would double-fire on machines that have both. There is no
platform predicate in `hooks.json` to gate it. I chose the option that
cannot double-export over the one that covers one more configuration.
Claude Code already requires Git for Windows for its own Bash tool, so this
is a narrow gap — but it is a gap, and it is now in the README.

**Tested:** `test_hooks_json_does_not_rely_on_the_shebang` walks every wired
event and asserts it goes through `hook.sh`, declares its shell, and passes
its event name. `test_launcher_runs_the_hook_end_to_end` runs the launcher
as Claude Code would and asserts the hook's real effect (the SessionEnd stop
file) plus a clean stdout. `test_launcher_exits_zero_and_silent_on_garbage`
and `test_launcher_says_so_when_no_interpreter_exists` (runs it with a PATH
containing coreutils but no Python, and asserts the breadcrumb).

## TRAP 5 — `_claude_code_pid()` and `ps`

**Done.** `platform_compat.parent_pid_of(pid)` is `ps -o ppid=` on POSIX and
a `CreateToolhelp32Snapshot` / `Process32First` / `Process32Next` walk via
`ctypes` on Windows. Toolhelp rather than `wmic` (removed in Windows 11
24H2) or PowerShell (a second of startup, in the session's critical path).

**The degradation is the important part.** The old code fell back to
`os.getppid()` — the transient shell — when `ps` failed. That shell exits
the moment `hook.py` does, so the pinger would see a dead parent on its
first iteration and exit having sent one ping: a session that silently
produces no heartbeats, which is a bug this code path has had before. And a
*recycled* pid would be worse: the pinger would watch a stranger.

`parent_pid_of` now returns **0 for unknown, never a fallback pid**, on both
platforms. `_claude_code_pid()` propagates the 0. `Pinger` treats
`watch_pid <= 0` as "run unwatched": it skips the liveness check entirely
and relies on the stop file and the 12-hour cap, and **logs that it is doing
so**. Bounded, and not silent.

**Tested:** `test_claude_code_pid_never_falls_back_to_the_dying_shell`
asserts the result is 0 *and* explicitly `!= os.getppid()`;
`..._uses_the_grandparent_when_known`; `..._survives_a_raising_lookup`;
`test_unwatched_pinger_keeps_running_instead_of_exiting` drives a real
`Pinger` with `watch_pid=0` and asserts it reaches its final stopped ping,
never probes pid 0, and says "unwatched" in the log. Windows snapshot logic
is tested with an injected `(pid, ppid)` list, including unknown-pid,
zero-parent, self-parent and raising-snapshot cases.

---

## Other POSIX assumptions found in the audit

**1. `$HOME` disagreement (silent, and it would have split the state
directory in two).** `hook.py` used `expanduser("~")` while `exporter.py`
preferred `os.environ["HOME"]`. Identical on POSIX. Under Git Bash on
Windows they are *not*: `$HOME` is an MSYS path like `/c/Users/me`, which a
native `python.exe` resolves against the current drive root as
`C:\c\Users\me`, while `ntpath.expanduser` ignores `HOME` entirely and
returns `USERPROFILE`. The hook and the exporter would have read and written
state in two different directories and neither would have said so. All four
entry points now call `platform_compat.home_dir(env)`: `$HOME` still wins
when it is a *native* absolute path (the test suite and env-scoped installs
depend on overriding it), then `USERPROFILE`, then `expanduser`. Tested for
the MSYS case, the native-override case and UNC.

**2. `config._is_usable_rule` rejected every Windows path — the biggest
silent no-op after the import errors.** It required `rule.startswith("/")`.
`/rius enable-here` writes the cwd verbatim, so on Windows it wrote
`C:\Users\me\proj`, the rule was discarded as unusable, `enable-here`
printed success, and tracing stayed off forever with no explanation. Now
accepts drive-absolute and UNC rules. The security intent is preserved in
the Windows spelling: a bare drive root (`C:\`, `C:/`) is rejected exactly
as `/` is, and a UNC needs both a server and a share.

`_rule_matches` also normalises separators and case **on Windows only**, via
`_normalise_for_match`. Windows paths are case-insensitive and either
separator works, so `C:\Proj` and `c:/proj` are one path; treating them as
two means a rule the user just wrote fails to match its own directory. This
only merges spellings the OS itself already considers identical, so it
cannot widen a rule beyond its own directory. POSIX behaviour is byte-
identical to before, and there is a test asserting POSIX stays
case-*sensitive*.

**3. Checked and fine, no change needed:** all path building already uses
`os.path.join` (`config`, `state`, `log`, `subagents.dir_for`); nothing
hard-codes `/` as a separator except the rule matcher above; there is no
`chmod` or file-mode logic in shipped code; `tempfile.mkstemp` usages in
`hook.py` and `state.save` both close the descriptor before the file is
renamed or handed to a child, which Windows requires; session ids are UUIDs,
so they are legal Windows filenames; `otlp.py` and `proto.py` are pure
`urllib`/bytes with no OS surface; `rius_ctl.py`'s `_most_recent_session`
uses `os.listdir` + `getmtime`, both portable.

## Constraints held

* **Zero runtime dependencies** — verified by running the CI job's own
  script locally: `scripts/` imports only the standard library.
  `sys.stdlib_module_names` contains `msvcrt`, `winreg` and `ctypes` even on
  Linux/macOS (checked directly), so that job's detection accepts them
  unchanged.
* **Python 3.9** — installed 3.9.25 and ran both the `runtime-floor` import
  check (14 modules parse and import) and the full suite: 253 passed, 2
  skipped, matching the pre-existing skip behaviour.
* Hooks still never exit non-zero and never write to stdout — asserted for
  `hook.sh` as well as `hook.py`.
* No API key in any new log line or message. `tests/test_content_never_in_state.py`
  and every other security test are untouched and passing.
* No `Span` is mutated after construction; nothing content-bearing was added
  to the state dict.
* `.github/workflows/ci.yml` untouched.

## Could not verify without a real Windows machine

Everything below is reasoned from documentation and source, and exercised
only through fakes:

1. **`msvcrt.locking` semantics in practice.** That a second `os.open` on
   the same path in the *same process* is refused is documented behaviour of
   Windows byte-range locks (they belong to the handle), and the fake models
   it — but the fake is my model, not Windows'. If Windows turns out to be
   more permissive here, `test_lock_is_exclusive`'s guarantee weakens to
   cross-process only. Consequence would be two exporters for one session
   processing the same transcript bytes, not data loss.
2. **The `ctypes` FFI plumbing itself.** `_kernel32()`'s argtypes/restypes
   and `_toolhelp_pairs()`'s `PROCESSENTRY32` layout are both marked
   `pragma: no cover - Windows only` and are the one part the fakes step
   over. A wrong struct field order or a truncated `HANDLE` would show up
   only on Windows. I set `restype = c_void_p` on every handle-returning
   call specifically because ctypes' default `c_int` truncates 64-bit
   handles; that is the most likely thing to be wrong if something is.
3. **Whether `bash "${CLAUDE_PLUGIN_ROOT}/..."` expands and quotes correctly
   under Git Bash**, given `CLAUDE_PLUGIN_ROOT` will contain backslashes.
   Backslashes inside double quotes are literal in bash and MSYS accepts
   Windows paths, so this should hold, but it is untested.
4. **That `DETACHED_PROCESS` children genuinely outlive the hook on
   Windows.** `test_e2e.py` proves it on POSIX only; there is no Windows
   equivalent.
5. **The Store-alias probe.** That `python3 -c ""` fails for a Microsoft
   Store stub is the documented behaviour of App Execution Aliases; I could
   not run one.
6. **Whether Claude Code honours `"shell": "bash"` on Windows** the way the
   docs describe, and what it does when Git Bash is absent.

## Residual risk

**With no `windows-latest` CI job, nothing executes any of this on Windows.**
The new tests cover the *logic* of every Windows branch — dispatch, failure
semantics, resource cleanup, ordering — by injecting fakes. What they cannot
cover, and what a real Windows run would be the first to exercise:

* the `ctypes` calls actually reaching kernel32 and returning what the
  structs claim (item 2 above) — this is the largest untested surface;
* real `msvcrt` lock contention between two real processes;
* real process detachment surviving the parent's exit;
* the hook being invoked *at all* by Claude Code on Windows — i.e. whether
  `shell: "bash"` + Git Bash + `hook.sh` is the right chain. If this is
  wrong, nothing else matters, and the symptom is the same silence the
  plugin has now. The `bootstrap.log` breadcrumb is the only thing that
  would distinguish "no Python" from "the hook never ran"; there is no
  breadcrumb for the latter, because nothing of ours executes.

Two failure modes I deliberately chose, both bounded:

* an unwatched pinger (`watch_pid == 0`) can outlive a crashed Claude Code
  by up to 12 hours, relying on the backend's stale→gone path. The
  alternative — falling back to the shell's pid — produces zero heartbeats
  every session, which is worse and silent;
* `replace_atomic` retries for ~200ms before raising. A `state.save` that
  loses the race for longer than that still raises, as it does today.

One behaviour change that reaches POSIX: `_claude_code_pid()` no longer
falls back to `os.getppid()` when `ps` fails. On POSIX `ps` is effectively
always present, so this should never trigger; when it does, the result is a
pinger that runs unwatched (and logs it) instead of one that exits
immediately. That is strictly the better failure.

---

# Deferred minors from the pre-merge review (carried forward verbatim)

The fix wave addressed C1, C2, I1, I2, I3, M1, M3, M4, M6, M10 and M12. These six
were knowingly deferred. Recorded here because they otherwise existed only in a
review transcript. None blocks merge; each is a real observation.

**M2 — the Windows locking tests assert the fake's semantics, not Windows'.**
`tests/test_platform_compat.py` and the re-run guarantees in `tests/test_state.py`
derive exclusivity from `FakeMsvcrt`'s `(st_dev, st_ino) -> fd` registry. What is
under test is the WIRING — that `try_lock` returns False on OSError, that
`session_lock` propagates it, that `unlock` is called. What is NOT under test is
that Windows actually refuses the second `os.open`. Raised so the test names are
not later mistaken for Windows verification. Closes with a real Windows CI job
(RIUS-945).

**M5 — `bootstrap.log` is unrotated, uncapped, and written unconditionally.**
`hook.sh`. Inconsistent with `rius_cc/log.py`, which writes date-stamped files
gated on `cfg.debug`. Three lines per hook event, and PreToolUse/PostToolUse fire
per tool call, so a user with no Python accumulates hundreds of lines per session.
Also the claim that it "distinguishes the failure cases" holds only one way: its
PRESENCE means "no interpreter found", but its ABSENCE is ambiguous between "hook
never ran", "hook ran fine" and "mkdir failed". Fix: write
`bootstrap-YYYY-MM-DD.log` to match `log.py`, or dedupe same-message-same-day.

**M7 — the glob spelling of a degenerate path rule is still accepted, on both
platforms.** `scripts/rius_cc/config.py`. A rule of `C:\*` passes `_is_usable_rule`
(`rule[3:].strip("\\/")` is `"*"`, truthy), normalises to `c:/*`, and fnmatch
matches across separators — enabling a whole drive with content capture. This
exactly mirrors the pre-existing POSIX behaviour of `/*`, so it is NOT a
regression. The issue is that `test_degenerate_windows_rules_never_match_anything`
enumerates `C:\`, `C:/`, `C:`, `\\`, `\\srv`, `//`, `*`, `**` and OMITS `C:\*` and
`C:/*`, so it reads as covering the degenerate cases when the glob spelling is
uncovered. Either add those literals and tighten `_is_usable_rule`, or add an
explicit "known-accepted" note covering both the POSIX and Windows spellings.
Related and smaller: `\\srv\share` (bare share root) is accepted, arguably as
broad as `C:\`.

**M8 — one microscopic POSIX behaviour change.** A rule of `//x` (double slash,
single component) was usable before and is now rejected by the UNC arm in
`config.py`. `//srv/share` and all ordinary `/...` rules are unaffected.
Effectively unreachable; noted only for completeness of the regression sweep.

**M9 — `IS_WINDOWS` is snapshotted in two places, so "simulate Windows" is not a
single switch.** `config.py` does `from .platform_compat import IS_WINDOWS`,
creating an independent copy; `tests/test_config.py`'s `as_windows` patches
`config.IS_WINDOWS` while every other fixture patches
`platform_compat.IS_WINDOWS`. Consequence: `test_detached_spawn_kwargs_reach_popen_on_windows`
runs with a "Windows" platform_compat and a POSIX config — a chimera. Fine for
what that test asserts, but no test can drive the whole system down the Windows
path, and the next person to try will be confused. Prefer reading
`platform_compat.IS_WINDOWS` at call time, or a `platform_compat.is_windows()`
accessor.

**M11 — `hooks.json` now requires `bash` on PATH.** Previously the command was the
script path itself, needing only the shebang and the exec bit. On a POSIX box
without bash (minimal Alpine, NixOS) hooks that work today would stop. `hook.sh`
is `#!/bin/sh`-clean, so `sh "${CLAUDE_PLUGIN_ROOT}/scripts/hook.sh"` would work
everywhere — but it may interact with Claude Code's `"shell": "bash"` field, so
verify before changing. Very low likelihood; noted for the regression sweep.

**Minor 3 — POSIX latency cost of the interpreter probe, and its mitigation.**
`scripts/_find_python.sh:24` (`uname -s`) and `:50` (the `candidate -c ""`
probe) now run on every hook event on macOS/Linux, including `PreToolUse` and
`PostToolUse`, which fire per tool call. On plain CPython this is roughly
20-40ms per event; under a `pyenv`/`asdf` shim, where `python3` is itself a
shell script, the probe roughly doubles the shim's own cost. This sits on the
session's critical path.

This is an accepted trade for platform-independent correctness, not a defect.
Before the probe, a broken or masquerading interpreter (e.g. the Windows
Store's `python3` alias, or a shim pointing at nothing) caused a non-zero hook
exit — which a hook must never do. The probe turns that failure mode into "try
the next candidate", at the cost of one extra process per candidate per hook
event, on every platform, forever.

Cheap mitigation if this ever bites in practice: skip the `uname -s` call
(`_find_python.sh:24`) when `$OS`, `$WINDIR` or `$windir` have already settled
the platform question (i.e. only fall through to `uname` when all three are
unset) — it is the one call of the two that is not load-bearing for
correctness, only for ordering the candidate list. The `-c ""` probe itself
should not be weakened; it is what makes the difference between "no
interpreter" and "silently traced nothing."
