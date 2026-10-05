"""Has the user approved this plugin's Codex hooks?

Codex runs no plugin hook until it is trusted in `/hooks`, which records
`[hooks.state."<plugin>@<marketplace>:codex/hooks.json:<event>:<i>:<j>"]`
with a `trusted_hash` in `$CODEX_HOME/config.toml`. Only presence is checked
here: the hash is Codex's own, so an entry left by an older version of the
plugin reads as approved even when Codex has since stopped trusting it.
"""
from __future__ import annotations

import json
import os
import re
from typing import List, Set

HOOKS_FILE = "codex/hooks.json"
_TABLE = re.compile(r'^\[hooks\.state\."rius@[^:"]+:' + re.escape(HOOKS_FILE)
                    + r':([a-z_]+):\d+:\d+"\]\s*$')
_TRUSTED = re.compile(r'^trusted_hash\s*=\s*"[^"]+"\s*$')


def _snake(event: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", event).lower()


def expected_events(plugin_root: str) -> List[str]:
    """The events our hooks file wires, as Codex names them in trust keys."""
    try:
        with open(os.path.join(plugin_root, HOOKS_FILE)) as fh:
            hooks = json.load(fh).get("hooks") or {}
    except (OSError, ValueError, AttributeError):
        return []
    return sorted(_snake(event) for event in hooks)


def approved_events(config_toml: str) -> Set[str]:
    try:
        with open(config_toml) as fh:
            lines = fh.read().splitlines()
    except OSError:
        return set()
    approved, table = set(), None
    for line in lines:
        line = line.strip()
        if line.startswith("["):
            match = _TABLE.match(line)
            table = match.group(1) if match else None
        elif table and _TRUSTED.match(line):
            approved.add(table)
    return approved


def status_line(plugin_root: str, codex_home: str) -> str:
    expected = expected_events(plugin_root)
    if not expected:
        return "Hooks: cannot read %s" % HOOKS_FILE
    approved = approved_events(os.path.join(codex_home, "config.toml"))
    count = len([e for e in expected if e in approved])
    if count == len(expected):
        return "Hooks: approved in /hooks (%d of %d)" % (count, count)
    return ("Hooks: %d of %d approved. Codex runs no plugin hook until you "
            "approve it: open /hooks in Codex and trust the rius hooks."
            % (count, len(expected)))
