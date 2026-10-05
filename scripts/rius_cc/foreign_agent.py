"""Recognise hook payloads that come from Cursor or Codex, not Claude Code.

Both agents run Claude Code plugin hooks: Cursor through its third-party
compatibility setting (on by default), Codex because it installs this
plugin from the same marketplace. Fed their payloads, hook.py would open an
empty "claude-code session" trace and start a heartbeat for a session that
is not a Claude Code session at all.

Only positive markers count, so a real Claude Code session is never
dropped. ``CURSOR_AGENT`` is deliberately NOT one: Cursor sets it for every
command its agent runs, so a ``claude`` started from Cursor's agent shell
carries it too. ``CURSOR_VERSION`` is only set in Cursor's hook environment.
"""
from __future__ import annotations

import os

CURSOR_PAYLOAD_KEYS = ("cursor_version", "conversation_id")
CURSOR_HOOK_ENV = "CURSOR_VERSION"


def is_foreign(payload: dict, env, home: str) -> bool:
    return _is_cursor(payload, env) or _is_codex(payload, env, home)


def _is_cursor(payload: dict, env) -> bool:
    if any(key in payload for key in CURSOR_PAYLOAD_KEYS):
        return True
    return bool(env.get(CURSOR_HOOK_ENV))


def _is_codex(payload: dict, env, home: str) -> bool:
    transcript = payload.get("transcript_path")
    if not isinstance(transcript, str) or not transcript:
        return False
    name = os.path.basename(transcript)
    if name.startswith("rollout-") and name.endswith(".jsonl"):
        return True
    # A CODEX_HOME set to a parent of Claude Code's home must not swallow
    # Claude Code's own transcripts.
    return (_is_within(transcript, _codex_home(env, home))
            and not _is_within(transcript, _claude_home(env, home)))


def _codex_home(env, home: str) -> str:
    return env.get("CODEX_HOME") or os.path.join(home, ".codex")


def _claude_home(env, home: str) -> str:
    return env.get("CLAUDE_CONFIG_DIR") or os.path.join(home, ".claude")


def _is_within(path: str, root: str) -> bool:
    path = os.path.normcase(os.path.abspath(path))
    root = os.path.normcase(os.path.abspath(root))
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:
        return False
