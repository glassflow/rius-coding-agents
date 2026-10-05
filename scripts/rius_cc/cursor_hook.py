"""What hook.py does for a Cursor hook: spool the event, say whether the
exporter should run, and answer Cursor on stdout.

Runs in the agent's critical path, so it only appends to files; reading the
spool and talking to the network are the detached exporter's job.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, Mapping, NamedTuple, Optional

from . import config, cursor_events, cursor_export, state

# Exported to the session by sessionStart's answer, so the /rius-* commands
# can name this conversation and find the plugin's scripts.
SESSION_ENV = "RIUS_CURSOR_SESSION_ID"
PLUGIN_ROOT_ENV = "RIUS_PLUGIN_ROOT"
CONTEXT_NOTE_PREFIX = "Rius plugin:"


class Job(NamedTuple):
    """An export the hook hands to the detached exporter. Ids only: no
    content travels through the hand-off file."""
    conversation_id: str
    cwd: str
    debug: bool

    def payload(self) -> Dict[str, str]:
        return {"conversation_id": self.conversation_id, "cwd": self.cwd}


def workspace_dir(payload: Dict[str, Any], env: Mapping[str, str]) -> str:
    """The folder path rules are matched against: the workspace, not the
    directory a single tool happened to run in."""
    roots = payload.get("workspace_roots")
    if isinstance(roots, list) and roots and isinstance(roots[0], str):
        return roots[0]
    return str(payload.get("cwd") or env.get("CURSOR_PROJECT_DIR") or "")


def response(event: str, payload: Any, plugin_root: str) -> str:
    """Cursor's answer: empty for every event, except that sessionStart
    hands the commands their session id and the plugin's location."""
    if event != "sessionStart" or not isinstance(payload, dict):
        return cursor_events.response_for(event)
    conversation_id = str(payload.get("conversation_id") or "")
    session_env = {PLUGIN_ROOT_ENV: plugin_root}
    if conversation_id:
        session_env[SESSION_ENV] = conversation_id
    return json.dumps({"env": session_env,
                       "additional_context": _context_note(session_env)})


def _context_note(session_env: Dict[str, str]) -> str:
    """The same values for the agent itself: Cursor applies a sessionStart
    `env` to later hooks, and may not pass it to the agent's Shell tool."""
    return ("%s plugin root %s, session id %s (used only by the /rius-* "
            "commands; ignore it for anything else)."
            % (CONTEXT_NOTE_PREFIX, session_env[PLUGIN_ROOT_ENV],
               session_env.get(SESSION_ENV, "unknown")))


# A subagent's first event is a tool call or a thought, never these.
_NEVER_FIRST_IN_A_SUBAGENT = ("sessionStart", "beforeSubmitPrompt",
                              "sessionEnd", "subagentStart", "subagentStop")


def link_headless_subagent(event: str, key: str, env: Mapping[str, str],
                           sdir: str) -> None:
    """Hang a subagent that fired no subagentStart under its parent.

    `cursor-agent -p` fires no subagentStart: a Task subagent's events
    arrive under the subagent's own conversation id with nothing that names
    the parent. The session env that sessionStart returned still names it,
    so a conversation that first shows up while a different one owns the
    session is that one's subagent.
    """
    parent = str(env.get(SESSION_ENV) or "")
    if (event in _NEVER_FIRST_IN_A_SUBAGENT or parent == key
            or not state.is_valid_session_id(parent)
            or os.path.exists(cursor_events.spool_path(sdir, key))
            or not os.path.exists(cursor_events.spool_path(sdir, parent))
            or cursor_export.root_conversation(sdir, key) != key):
        return
    cursor_export.link_subagent(sdir, key, parent)


def _spool(event: str, payload: Dict[str, Any], sdir: str, cfg) -> int:
    """Append the event; returns how many tools the conversation finished."""
    cursor_events.record(payload, sdir, cfg.capture_content,
                         cfg.max_attr_bytes)
    subagent_id = str(payload.get("subagent_id") or "")
    if event == "subagentStart" and state.is_valid_session_id(subagent_id):
        cursor_export.link_subagent(sdir, subagent_id,
                                    cursor_events.spool_key(payload))
    if event in cursor_export.TOOL_DONE_EVENTS:
        return cursor_export.tick(sdir, cursor_events.spool_key(payload))
    return 0


def _closes_open_trace(event: str, conversation_id: str, home: str) -> bool:
    """A folder disabled mid-session still has its trace closed."""
    return event == "sessionEnd" and state.trace_is_open(
        state.load(conversation_id, home))


def handle(event: str, payload: Any, env: Mapping[str, str],
           home: str) -> Optional[Job]:
    """Spool one hook event. Returns the exporter's job when one is due."""
    if not isinstance(payload, dict):
        return None
    payload.setdefault("hook_event_name", event)
    sdir = cursor_export.spool_dir(home)
    # Conversation ids are UUIDs; tool call ids are not, and are never
    # checked here.
    key = cursor_events.spool_key(payload)
    if not state.is_valid_session_id(key):
        return None
    link_headless_subagent(event, key, env, sdir)
    root = cursor_export.root_conversation(sdir, key)
    if not state.is_valid_session_id(root):
        return None
    cwd = workspace_dir(payload, env)
    cfg = config.resolve(root, cwd, env, home)
    if not cfg.api_key:
        return None
    tools_done = 0
    if cfg.enabled:
        tools_done = _spool(event, payload, sdir, cfg)
    elif not _closes_open_trace(event, root, home):
        return None
    if not cursor_export.export_due(event, tools_done):
        return None
    return Job(root, cwd, bool(cfg.debug))
