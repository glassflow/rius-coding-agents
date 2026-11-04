"""The agent a named session files under.

A name the user gave the session (`--name`, `/rename`) is the trace's title
and the agent it belongs to, so the trace list, the Agents filter and the
Agents page all call it the same thing. Claude Code's own summary title is
not a name anyone chose, so it never becomes an agent.

The state file holds a custom title only while content capture is on
(spans.note_titles), so with capture off there is no agent name and the
backend falls back to the service name.

Imports nothing from rius_cc: the heartbeat pinger reads it without pulling
in the span builder.
"""
from __future__ import annotations

# The backend rejects a heartbeat whose agent_name is longer than this, in bytes.
AGENT_NAME_MAX_BYTES = 256


def agent_name(st: dict) -> str:
    title = st.get("title_custom")
    if not isinstance(title, str):
        return ""
    return title.encode("utf-8")[:AGENT_NAME_MAX_BYTES].decode(
        "utf-8", errors="ignore").strip()
