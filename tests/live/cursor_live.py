#!/usr/bin/env python3
"""Run Cursor for real against the Rius plugin and check the traces it sent.

  cursor_live.py list | all | <scenario> | serve | cleanup

Needs a paid Cursor plan; see README.md for what it touches in the home.
"""
from __future__ import annotations

import datetime
import functools
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from typing import Callable, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import harness  # noqa: E402
import receiver as receiver_mod  # noqa: E402
import trace_shape  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(HERE))
RUNS = os.path.join(HERE, "runs")
TIMEOUT = 180
SETTLE = 30
STARTUP = 8
MODEL_PREFERENCE = ("gemini-3.7-flash-high", "composer-2.5", "auto")
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")

PROMPT_SHELL = ("Run the shell command `true`, then run the shell command `false`, "
                "as two separate shell tool calls. Then say done.")
PROMPT_SUBAGENT = ("Use the Task tool to start a subagent that runs the shell command "
                   "`echo hello` and reports what it printed. Then say done.")
PROMPT_RESUME = "Run the shell command `echo again`, then say done."
PROMPT_CONTENT = "Run the shell command `echo live-content-check`, then say done."
PROMPT_TOOLS = ("Run the shell command `true`. Then use the Task tool to start a subagent "
                "that runs `echo hello`. Then say done.")
PROMPT_TOKENS = "Reply with the single word ok."


class Ctx:
    def __init__(self, run_dir, rcv, session, model):
        self.run_dir = run_dir
        self.rcv = rcv
        self.session = session
        self.model = model
        self.state: Dict[str, object] = {}
        self.notes: List[str] = []
        self.require_tokens = os.environ.get("CURSOR_LIVE_REQUIRE_TOKENS") == "1"


def strip_ansi(text: str) -> str:
    return ANSI.sub("", text).replace("\r", "")


def choose_model(listing: str, override: str = "") -> str:
    """`CURSOR_LIVE_MODEL`, else the first of MODEL_PREFERENCE the account
    lists: the listing has no prices, so the cheap models are named."""
    if override:
        return override
    ids = {line.split(" - ")[0].strip() for line in strip_ansi(listing).splitlines()}
    return next((m for m in MODEL_PREFERENCE if m in ids), "auto")


def _kill(proc: subprocess.Popen) -> None:
    try:
        if hasattr(os, "killpg"):
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except OSError:
        pass


def run_process(argv: List[str], cwd: str, env: Optional[Dict[str, str]] = None
                ) -> Tuple[Optional[int], str]:
    """(exit code, output); the code is None when it ran past TIMEOUT."""
    proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, errors="replace", start_new_session=True)
    try:
        out, _ = proc.communicate(timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        _kill(proc)
        out, _ = proc.communicate()
        return None, out
    except BaseException:
        _kill(proc)     # its own session hides Ctrl-C from it
        proc.wait()
        raise
    return proc.returncode, out


def agent_argv(ctx: Ctx, folder: str, *extra: str) -> List[str]:
    return ["cursor-agent", "--model", ctx.model, "--force", "--trust",
            "--workspace", folder] + list(extra)


def headless(ctx: Ctx, prompt: str, folder: str, resume: str = "") -> List[str]:
    argv = agent_argv(ctx, folder, "-p", "--output-format", "json")
    argv += ["--resume", resume] if resume else []
    code, out = run_process(argv + [prompt], folder)
    if code is None:
        return ["cursor-agent ran past %d s" % TIMEOUT]
    if code != 0:
        return ["cursor-agent exited %d: %s" % (code, out.strip()[-300:])]
    return []


def pty_session(argv: List[str], cwd: str, prompt: str, done: Callable[[str], bool],
                env: Optional[Dict[str, str]] = None) -> str:
    """Drive an interactive cursor-agent: type `prompt` once it has drawn its
    screen, read until `done(output)` or TIMEOUT, then quit. Returns what it
    drew, with the escape codes taken out."""
    import pty
    import select
    master, slave = pty.openpty()
    proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=slave, stdout=slave,
                            stderr=slave, start_new_session=True)
    os.close(slave)
    chunks: List[str] = []
    started, typed = time.monotonic(), False
    try:
        while time.monotonic() - started < TIMEOUT and proc.poll() is None:
            if select.select([master], [], [], 0.5)[0]:
                try:
                    chunks.append(os.read(master, 65536).decode("utf-8", "replace"))
                except OSError:
                    break
            if not typed and time.monotonic() - started > STARTUP:
                os.write(master, (prompt + "\r").encode())
                typed = True
            elif typed and done(strip_ansi("".join(chunks))):
                break
    finally:
        _quit(proc, master)
    return strip_ansi("".join(chunks))


def _quit(proc: subprocess.Popen, master: int) -> None:
    try:
        os.write(master, b"\x03\x03")
        proc.wait(timeout=8)
    except (OSError, subprocess.TimeoutExpired):
        _kill(proc)
    os.close(master)


def spool_events(ctx: Ctx) -> List[str]:
    spool = os.path.join(ctx.session.layout.rius, "spool")
    names: List[str] = []
    if os.path.isdir(spool):
        for name in sorted(os.listdir(spool)):
            if name.endswith(".jsonl"):
                with open(os.path.join(spool, name), encoding="utf-8") as fh:
                    names += trace_shape.spool_events(fh.read().splitlines())
    return names


def settle(ctx: Ctx, offset: int, check: Callable) -> List[str]:
    """The exporter runs detached, so poll until the spans satisfy `check`."""
    deadline = time.monotonic() + SETTLE
    while True:
        lines = ctx.rcv.lines()[offset:]
        problems = check(trace_shape.merge(trace_shape.decode(lines)))
        if not problems or time.monotonic() >= deadline:
            return problems
        time.sleep(1)


def one_shot(ctx: Ctx, prompt: str, folder: str, check: Callable, resume: str = "") -> List[str]:
    offset = len(ctx.rcv.lines())
    ctx.state["offset"] = offset
    return headless(ctx, prompt, folder, resume) + settle(ctx, offset, check)


def sc_headless_shell(ctx):
    return one_shot(ctx, PROMPT_SHELL, ctx.session.structure, trace_shape.check_shell)


def sc_headless_subagent(ctx):
    problems = one_shot(ctx, PROMPT_SUBAGENT, ctx.session.structure, trace_shape.check_subagent)
    ctx.state["subagent_offset"] = ctx.state["offset"]
    return problems


def _chat_id(ctx: Ctx) -> str:
    spans = trace_shape.decode(ctx.rcv.lines()[ctx.state["subagent_offset"]:])
    roots = [s for s in spans if s["kind"] == "AGENT" and not s["parent_id"]]
    return str(roots[0]["attrs"].get("session.id", "")) if roots else ""


def sc_headless_resume(ctx):
    if "subagent_offset" not in ctx.state:
        seeded = sc_headless_subagent(ctx)
        if seeded:
            return ["the run to resume failed: " + "; ".join(seeded)]
    chat = _chat_id(ctx)
    if not chat:
        return ["no chat id on the root span to resume"]
    ran = headless(ctx, PROMPT_RESUME, ctx.session.structure, chat)
    return ran + settle(ctx, ctx.state["subagent_offset"], trace_shape.check_resume)


def sc_content(ctx):
    return one_shot(ctx, PROMPT_CONTENT, ctx.session.content, trace_shape.check_content)


def interactive(ctx: Ctx, prompt: str, done: Callable[[str], bool],
                check: Callable, env: Optional[Dict[str, str]] = None) -> Tuple[List[str], str]:
    if sys.platform == "win32":
        return ["interactive scenarios need a POSIX pty"], ""
    offset = len(ctx.rcv.lines())
    full_env = dict(os.environ, **(env or {}))
    folder = ctx.session.structure
    output = pty_session(agent_argv(ctx, folder), folder, prompt, done, full_env)
    return settle(ctx, offset, check), output


def _stopped(ctx: Ctx) -> Callable[[str], bool]:
    before = spool_events(ctx).count("stop")
    return lambda _output: spool_events(ctx).count("stop") > before


def _with_spool_events(ctx: Ctx, problems: List[str]) -> List[str]:
    return problems + trace_shape.check_interactive_events(spool_events(ctx))


def sc_interactive_tools(ctx):
    problems, _ = interactive(ctx, PROMPT_TOOLS, _stopped(ctx),
                              trace_shape.check_interactive_tools)
    return _with_spool_events(ctx, problems)


def sc_interactive_status(ctx):
    problems, output = interactive(ctx, "/rius-status", lambda out: "Rius tracing" in out,
                                   lambda spans: [])
    return problems + ([] if "Rius tracing: on" in output
                       else ["no `Rius tracing: on` in the output: %s" % output.strip()[-300:]])


def sc_tokens(ctx):
    dump = os.path.join(ctx.run_dir, "stop-payloads.jsonl")
    command = '"%s" "%s"' % (sys.executable, os.path.join(HERE, "dump_hook.py"))
    ctx.session.add_hook("stop", command)
    check = functools.partial(trace_shape.check_tokens, required=ctx.require_tokens)
    problems, _ = interactive(ctx, PROMPT_TOKENS, _stopped(ctx), check,
                              {"RIUS_LIVE_DUMP": dump})
    return problems + _report_stop_keys(ctx, dump)


def _report_stop_keys(ctx: Ctx, dump: str) -> List[str]:
    if not os.path.exists(dump):
        return ["the dump hook never ran: no raw stop payload to report"]
    with open(dump, encoding="utf-8") as fh:
        rows = [json.loads(line) for line in fh if line.strip()]
    keys = sorted({k for row in rows for k in row["keys"]})
    numbers = sorted({k for row in rows for k in row["numbers"]})
    ctx.notes.append("raw `stop` payload keys: %s; numeric: %s" % (keys, numbers))
    return []


SCENARIOS: Dict[str, Callable] = {
    "headless_shell": sc_headless_shell,
    "headless_subagent": sc_headless_subagent,
    "headless_resume": sc_headless_resume,
    "content": sc_content,
    "interactive_tools": sc_interactive_tools,
    "interactive_status": sc_interactive_status,
    "tokens": sc_tokens,
}


def run_scenario(ctx: Ctx, name: str) -> List[str]:
    attempts = 2 if name.startswith("interactive") or name == "tokens" else 1
    problems: List[str] = []
    for _ in range(attempts):
        try:
            problems = SCENARIOS[name](ctx)
        except harness.Abort as exc:
            return [str(exc)]
        if not problems:
            break
    return problems


def no_signal_at_all(ctx: Ctx, problems: List[str]) -> bool:
    log = os.path.join(ctx.session.layout.rius, "log")
    return (trace_shape.NO_SPAN in problems
            and not (os.path.isdir(log) and os.listdir(log)))


def run_all(ctx: Ctx, names: List[str]) -> Dict[str, List[str]]:
    results: Dict[str, List[str]] = {}
    loading = "plugin symlink"
    for name in names:
        problems = run_scenario(ctx, name)
        if loading == "plugin symlink" and not results and no_signal_at_all(ctx, problems):
            ctx.session.install_hooks()
            loading = "install-hooks fallback"
            problems = run_scenario(ctx, name)
        results[name] = problems
        print(("FAIL %s: %s" % (name, "; ".join(problems))) if problems else "PASS %s" % name,
              flush=True)
    ctx.notes.insert(0, "loaded through: %s" % loading)
    return results


def summary(results: Dict[str, List[str]], ctx: Ctx, version: str) -> str:
    rows = ["| scenario | result |", "|---|---|"]
    for name, problems in results.items():
        rows.append("| %s | %s |" % (name, "PASS" if not problems else "FAIL: " + "; ".join(problems)))
    head = ["# Live Cursor run", "",
            "- cursor-agent: %s" % version, "- model: %s" % ctx.model,
            "- date: %s" % datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")]
    return "\n".join(head + ["- " + n for n in ctx.notes] + [""] + rows) + "\n"


def listing(path: str) -> str:
    if sys.platform == "win32":
        return "\n".join(sorted(os.listdir(path)))
    return subprocess.run(["ls", "-la", path], capture_output=True, text=True).stdout


def real_layout() -> harness.Layout:
    marker = os.path.join(ROOT, harness.TRUST_MARKER)
    if os.path.exists(marker):
        raise harness.Abort("%s exists; remove it first (a pytest run is not over)" % marker)
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    from rius_cc import platform_compat
    return harness.Layout(platform_compat.home_dir(), ROOT)


def ctl_runner(layout: harness.Layout) -> harness.Ctl:
    script = os.path.join(layout.root, "scripts", "rius_ctl.py")

    def run(args: List[str], stdin: Optional[str]) -> Tuple[int, str]:
        done = subprocess.run([sys.executable, script] + args, input=stdin,
                              capture_output=True, text=True, timeout=60)
        return done.returncode, done.stdout + done.stderr
    return run


def _agent_text(*args: str) -> str:
    done = subprocess.run(["cursor-agent"] + list(args), capture_output=True, text=True, timeout=60)
    return done.stdout + done.stderr


def _on_sigterm(_signum, _frame):
    sys.exit(143)


def live(names: Optional[List[str]], serve: bool) -> int:
    try:
        layout = real_layout()
    except harness.Abort as exc:
        print("abort:", exc, file=sys.stderr)
        return 2
    status = "Logged in (serve)" if serve else strip_ansi(_agent_text("status"))
    problems = harness.preflight(layout, status)
    if problems:
        print("\n".join("abort: " + p for p in problems), file=sys.stderr)
        return 2
    run_dir = os.path.join(RUNS, datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    os.makedirs(run_dir)
    cursor_dir = os.path.dirname(layout.rius)
    with open(os.path.join(run_dir, "home-before.txt"), "w") as fh:
        fh.write(listing(cursor_dir))
    try:
        rcv = receiver_mod.Receiver(run_dir)
    except receiver_mod.PortBusy as exc:
        print("abort:", exc, file=sys.stderr)
        return 2
    rcv.start()
    session = harness.Session(layout, ctl_runner(layout))
    for name in ("SIGTERM", "SIGHUP"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), _on_sigterm)
    code = 1
    try:
        session.setup()
        code = _serve(session, rcv) if serve else _scenarios(session, rcv, run_dir, names or [])
    except harness.Abort as exc:
        print("abort:", exc, file=sys.stderr)
        code = 2
    except KeyboardInterrupt:
        code = 130
    finally:
        try:
            left = session.teardown(run_dir)
        finally:
            rcv.stop()
            with open(os.path.join(run_dir, "home-after.txt"), "w") as fh:
                fh.write(listing(cursor_dir))
    if left:
        print("LEFT BEHIND: %s\nrun `cursor_live.py cleanup`" % left, file=sys.stderr)
        return 1
    print("run dir: %s" % run_dir)
    return code


def _scenarios(session, rcv, run_dir: str, names: List[str]) -> int:
    version = _agent_text("--version").strip()
    model = choose_model(_agent_text("--list-models"), os.environ.get("CURSOR_LIVE_MODEL", ""))
    print("cursor-agent %s, model %s" % (version, model), flush=True)
    ctx = Ctx(run_dir, rcv, session, model)
    results = run_all(ctx, names)
    with open(os.path.join(run_dir, "summary.md"), "w", encoding="utf-8") as fh:
        fh.write(summary(results, ctx, version))
    return 0 if not any(results.values()) else 1


def _serve(session, rcv) -> int:
    print("structure folder: %s\ncontent folder:   %s\nrun cursor-agent in either; Ctrl-C to stop"
          % (session.structure, session.content), flush=True)
    last = -1
    while True:
        if rcv.requests != last:
            last = rcv.requests
            spans = trace_shape.merge(trace_shape.decode(rcv.lines()))
            print("%d requests, %d spans" % (rcv.requests, len(spans)), flush=True)
        time.sleep(1)


def cleanup() -> int:
    try:
        layout = real_layout()
    except harness.Abort as exc:
        print("abort:", exc, file=sys.stderr)
        return 2
    left = harness.teardown(layout)
    print("left behind: %s" % left if left else "clean")
    return 1 if left else 0


def main(argv: List[str]) -> int:
    if len(argv) != 2 or argv[1] not in list(SCENARIOS) + ["all", "list", "serve", "cleanup"]:
        print("usage: cursor_live.py list | all | serve | cleanup | <scenario>\n"
              "scenarios: %s" % " ".join(SCENARIOS), file=sys.stderr)
        return 2
    command = argv[1]
    if command == "list":
        print("\n".join(SCENARIOS))
        return 0
    if command == "cleanup":
        return cleanup()
    return live(None if command == "serve" else list(SCENARIOS) if command == "all" else [command],
                serve=command == "serve")


if __name__ == "__main__":
    sys.exit(main(sys.argv))
