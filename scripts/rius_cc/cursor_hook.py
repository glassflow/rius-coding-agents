"""What hook.py does for a Cursor hook: spool the event, say whether the
exporter should run, and answer Cursor on stdout.

Runs in the agent's critical path, so it only appends to files; reading the
spool and talking to the network are the detached exporter's job.
"""
from __future__ import annotations

import json
from typing import Any, Dict, Mapping, NamedTuple, Optional

from . import config, cursor_events, cursor_export, state

# Exported to the session by sessionStart's answer, so the /rius-* commands
# can name this conversation and find the plugin's scripts.
SESSION_ENV = "RIUS_CURSOR_SESSION_ID"
PLUGIN_ROOT_ENV = "RIUS_PLUGIN_ROOT"


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
    return json.dumps({"env": session_env})


def _spool(event: str, payload: Dict[str, Any], sdir: str, cfg) -> int:
    """Append the event; returns how many tools the conversation finished."""
    cursor_events.record(payload, sdir, cfg.capture_content,
                         cfg.max_attr_bytes)
    if event == "subagentStart" and payload.get("subagent_id"):
        cursor_export.link_subagent(sdir, str(payload["subagent_id"]),
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
    key = cursor_events.spool_key(payload)
    if not key:
        return None
    root = cursor_export.root_conversation(sdir, key)
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
