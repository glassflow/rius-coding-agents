"""Best-effort debug log, shared by every entry point.

One place so that a process which fails silently (the heartbeat pinger, the
detached exporter) can always say WHY in ``~/.claude/rius/log/``. Never
raises -- a logger that can break the session it observes is worse than no
logger -- and never writes an API key: every message is passed through
``config.redact`` for the key it was given.
"""
from __future__ import annotations

import datetime
import os

from . import agent, config, platform_compat


def log_dir(home: str) -> str:
    return agent.active().log_dir(home)


def write(home: str, cfg, message: str, force: bool = False) -> None:
    """Append one line. No-op unless cfg.debug (or force=True).

    ``force`` exists for unhandled exceptions only: a crash you cannot see is
    the failure mode this whole module exists to prevent, so those are logged
    even with RIUS_CLAUDE_DEBUG off. Documented in README > Settings.
    """
    if not force and not getattr(cfg, "debug", False):
        return
    try:
        text = message
        api_key = getattr(cfg, "api_key", None)
        if api_key:
            text = text.replace(api_key, config.redact(api_key))
        d = platform_compat.rius_dir(home, "log")
        # timezone-aware: datetime.utcnow() is deprecated from 3.12.
        date = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
        with platform_compat.open_private_append(os.path.join(d, date + ".log")) as fh:
            fh.write(text.rstrip("\n") + "\n")
    except BaseException:
        pass
