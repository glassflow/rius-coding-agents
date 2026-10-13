"""Setup and teardown of the live Cursor harness.

Everything it puts in the home it records in the sentinel file
(~/.cursor/rius/.rius-live-harness), so `teardown` undoes exactly that, also
after a crash: `cursor_live.py cleanup` runs the same code. `home` and `root`
are parameters so the tests can point it at a temporary home.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from typing import Callable, Dict, List, Optional, Tuple

SENTINEL = ".rius-live-harness"
DUMMY_KEY = "ri_" + "x" * 24
TRUST_MARKER = os.path.join("scripts", "rius_cc", "_tests_trust_env_home.py")
LOGGED_IN = "Logged in"

Ctl = Callable[[List[str], Optional[str]], Tuple[int, str]]


class Abort(Exception):
    pass


class Layout:
    def __init__(self, home: str, root: str) -> None:
        self.home = home
        self.root = root
        cursor = os.path.join(home, ".cursor")
        self.rius = os.path.join(cursor, "rius")
        self.sentinel = os.path.join(self.rius, SENTINEL)
        self.plugins = os.path.join(cursor, "plugins")
        self.plugins_local = os.path.join(self.plugins, "local")
        self.link = os.path.join(self.plugins_local, "rius")
        self.hooks_json = os.path.join(cursor, "hooks.json")
        self.marker = os.path.join(root, TRUST_MARKER)


def preflight(layout: Layout, status_output: str) -> List[str]:
    """Why the harness must not start; empty when it may."""
    problems = []
    if os.path.exists(layout.marker):
        problems.append("%s exists: the test suite's marker makes the plugin trust "
                        "$HOME. Remove it (a pytest run is not over)" % layout.marker)
    if os.path.exists(layout.sentinel):
        problems.append("%s is left over from an earlier run: run "
                        "`cursor_live.py cleanup`" % layout.rius)
    elif os.path.lexists(layout.rius):
        problems.append("%s exists and is not the harness's: it may hold a real key"
                        % layout.rius)
    if os.path.lexists(layout.link):
        problems.append("%s exists: the real plugin is installed there" % layout.link)
    if LOGGED_IN not in status_output:
        problems.append("cursor-agent is not logged in: %s" % status_output.strip()[:200])
    return problems


def _read_state(layout: Layout) -> Dict:
    try:
        with open(layout.sentinel, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


class Session:
    def __init__(self, layout: Layout, ctl: Ctl, scratch: Optional[str] = None) -> None:
        self.layout = layout
        self.ctl = ctl
        self.scratch = scratch or tempfile.gettempdir()
        self.state: Dict = {"created_dirs": [], "folders": [], "hooks_json": False}
        self.structure = ""
        self.content = ""

    def _save(self) -> None:
        with open(self.layout.sentinel, "w", encoding="utf-8") as fh:
            json.dump(self.state, fh)

    def _make_dir(self, path: str) -> None:
        if not os.path.isdir(path):
            os.mkdir(path)
            self.state["created_dirs"].append(path)
            self._save()

    def setup(self) -> None:
        layout = self.layout
        os.makedirs(layout.rius, mode=0o700)
        self._save()
        for path in (layout.plugins, layout.plugins_local):
            self._make_dir(path)
        os.symlink(layout.root, layout.link)
        self.state["link"] = layout.link
        self._save()
        self._ctl_ok(["use-key", "--agent", "cursor", "--env", "local"], DUMMY_KEY)
        self.structure = self._folder("structure", "enable-here")
        self.content = self._folder("content", "content-on-here")

    def _folder(self, name: str, action: str) -> str:
        path = os.path.realpath(tempfile.mkdtemp(prefix="rius-live-%s-" % name,
                                                 dir=self.scratch))
        self.state["folders"].append(path)
        self._save()
        self._ctl_ok([action, "--agent", "cursor", "--cwd", path], None)
        return path

    def _ctl_ok(self, args: List[str], stdin: Optional[str]) -> None:
        code, out = self.ctl(args, stdin)
        if code != 0:
            raise Abort("rius_ctl.py %s failed (%d): %s" % (args[0], code, out.strip()[:300]))

    def claim_hooks_json(self) -> None:
        """~/.cursor/hooks.json is the user's. The harness only ever makes it
        itself, and refuses when one is already there."""
        if self.state["hooks_json"]:
            return
        if os.path.lexists(self.layout.hooks_json):
            raise Abort("%s exists; the harness will not change it" % self.layout.hooks_json)
        self.state["hooks_json"] = True
        self._save()

    def install_hooks(self) -> None:
        self.claim_hooks_json()
        self._ctl_ok(["install-hooks", "--agent", "cursor"], None)

    def add_hook(self, event: str, command: str) -> None:
        self.claim_hooks_json()
        try:
            with open(self.layout.hooks_json, encoding="utf-8") as fh:
                doc = json.load(fh)
        except OSError:
            doc = {"version": 1, "hooks": {}}
        doc.setdefault("hooks", {}).setdefault(event, []).append({"command": command})
        with open(self.layout.hooks_json, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2)

    def wait_quiet(self, quiet: float = 3.0, maximum: float = 20.0) -> None:
        wait_quiet(self.layout.rius, quiet, maximum)

    def collect(self, run_dir: str) -> None:
        collect(self.layout.rius, run_dir)

    def teardown(self, run_dir: Optional[str] = None, settle: float = 1.0) -> List[str]:
        if run_dir and os.path.exists(self.layout.sentinel):
            self.wait_quiet()
            self.collect(run_dir)
        return teardown(self.layout, settle)


def wait_quiet(rius_dir: str, quiet: float, maximum: float) -> None:
    """The exporter runs detached and may still be writing: wait until the
    rius dir has not changed for `quiet` seconds, so it cannot recreate the
    dir after it is removed."""
    deadline = time.monotonic() + maximum
    seen, since = _snapshot(rius_dir), time.monotonic()
    while time.monotonic() < deadline and time.monotonic() - since < quiet:
        time.sleep(0.25)
        now = _snapshot(rius_dir)
        if now != seen:
            seen, since = now, time.monotonic()


def _snapshot(path: str) -> List[Tuple[str, int, int]]:
    out = []
    for base, _, names in os.walk(path):
        for name in names:
            try:
                st = os.stat(os.path.join(base, name))
            except OSError:
                continue
            out.append((os.path.join(base, name), st.st_size, int(st.st_mtime_ns)))
    return sorted(out)


def collect(rius_dir: str, run_dir: str) -> None:
    for sub, pattern in (("spool", ".jsonl"), ("log", "")):
        source = os.path.join(rius_dir, sub)
        if not os.path.isdir(source):
            continue
        target = os.path.join(run_dir, sub)
        os.makedirs(target, exist_ok=True)
        for name in sorted(os.listdir(source)):
            if name.endswith(pattern) and os.path.isfile(os.path.join(source, name)):
                shutil.copy2(os.path.join(source, name), os.path.join(target, name))


def teardown(layout: Layout, settle: float = 1.0) -> List[str]:
    """Undo what the sentinel records. Returns what is still there."""
    if not os.path.exists(layout.sentinel):
        return []
    state = _read_state(layout)
    if state.get("link") and os.path.islink(layout.link):
        os.unlink(layout.link)
    for path in state.get("folders", []):
        shutil.rmtree(path, ignore_errors=True)
    if state.get("hooks_json") and os.path.lexists(layout.hooks_json):
        os.unlink(layout.hooks_json)
    shutil.rmtree(layout.rius, ignore_errors=True)
    time.sleep(settle)
    shutil.rmtree(layout.rius, ignore_errors=True)
    for path in reversed(state.get("created_dirs", [])):
        try:
            os.rmdir(path)
        except OSError:
            pass
    return _leftovers(layout, state)


def _leftovers(layout: Layout, state: Dict) -> List[str]:
    paths = [layout.rius, layout.link] + state.get("folders", []) + state.get("created_dirs", [])
    if state.get("hooks_json"):
        paths.append(layout.hooks_json)
    return [p for p in paths if os.path.lexists(p)]
