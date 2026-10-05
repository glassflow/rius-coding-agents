"""Write the plugin's Cursor hooks into a user's own hooks.json.

For Cursor builds that do not run hooks shipped in plugins (CLI releases
before 2026-08-27). The entries come from cursor/hooks.json, the file the
plugin itself ships, with the plugin root spelled out: a user's hooks.json
has no ${CURSOR_PLUGIN_ROOT}.

The file is merged, never replaced: other hooks stay as they are. Rius
entries are recognised by their command, so installing twice changes
nothing and installing after an upgrade (a new plugin path) replaces the
stale entries instead of adding a second set.
"""
from __future__ import annotations

import json
import os
import tempfile
from typing import Any, Dict, List, Tuple

from . import platform_compat

ROOT_VARIABLE = "${CURSOR_PLUGIN_ROOT}"
RIUS_MARKER = '/scripts/hook.sh" --agent cursor '


class HooksFileError(ValueError):
    pass


def plugin_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))


def shipped_hooks(root: str) -> Dict[str, List[Dict[str, Any]]]:
    """cursor/hooks.json's hooks, pointing at `root`."""
    with open(os.path.join(root, "cursor", "hooks.json")) as fh:
        hooks = json.load(fh)["hooks"]
    return {event: [dict(e, command=e["command"].replace(ROOT_VARIABLE, root))
                    for e in entries]
            for event, entries in hooks.items()}


def is_rius_entry(entry: Any) -> bool:
    return isinstance(entry, dict) and RIUS_MARKER in str(entry.get("command"))


def _without_rius(doc: Dict[str, Any]) -> Dict[str, Any]:
    hooks = {}
    for event, entries in (doc.get("hooks") or {}).items():
        kept = [e for e in entries if not is_rius_entry(e)]
        if kept:
            hooks[event] = kept
    return dict(doc, hooks=hooks)


def _rius_entries(doc: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    found = {}
    for event, entries in (doc.get("hooks") or {}).items():
        mine = [e for e in entries if is_rius_entry(e)]
        if mine:
            found[event] = mine
    return found


def merge(doc: Dict[str, Any], ours: Dict[str, List[Dict[str, Any]]]):
    if _rius_entries(doc) == ours:
        return doc
    merged = _without_rius(doc)
    merged.setdefault("version", 1)
    for event, entries in ours.items():
        merged["hooks"][event] = merged["hooks"].get(event, []) + entries
    return merged


def remove(doc: Dict[str, Any]) -> Dict[str, Any]:
    return _without_rius(doc)


def _is_hooks_doc(doc: Any) -> bool:
    if not isinstance(doc, dict) or not isinstance(doc.get("hooks", {}), dict):
        return False
    return all(isinstance(v, list) for v in (doc.get("hooks") or {}).values())


def _load(path: str) -> Dict[str, Any]:
    try:
        with open(path) as fh:
            text = fh.read()
    except FileNotFoundError:
        return {"version": 1, "hooks": {}}
    try:
        doc = json.loads(text) if text.strip() else {"version": 1, "hooks": {}}
    except ValueError as exc:
        raise HooksFileError("%s is not valid JSON (%s); left unchanged"
                             % (path, exc))
    if not _is_hooks_doc(doc):
        raise HooksFileError("%s does not look like a Cursor hooks.json; "
                             "left unchanged" % path)
    return doc


def _write(path: str, doc: Dict[str, Any]) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".hooks-", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(doc, fh, indent=2)
            fh.write("\n")
        if os.path.exists(path):
            os.chmod(tmp, os.stat(path).st_mode & 0o777)
        platform_compat.replace_atomic(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _apply(path: str, change) -> Tuple[bool, int]:
    """(changed, number of Rius entries now in the file)."""
    before = _load(path)
    after = change(before)
    count = sum(1 for entries in after["hooks"].values()
                for e in entries if is_rius_entry(e))
    if after == before:
        return False, count
    _write(path, after)
    return True, count


def install(path: str, root: str) -> Tuple[bool, int]:
    ours = shipped_hooks(root)
    return _apply(path, lambda doc: merge(doc, ours))


def uninstall(path: str) -> Tuple[bool, int]:
    return _apply(path, remove)
