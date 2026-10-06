"""Which coding agent this process serves, and everything that differs by it.

One process serves exactly one agent, chosen by `--agent` on its command
line (default: Claude Code) and fixed for the life of the process. So the
profile is process-wide rather than threaded through every path helper:
those helpers take `home` in dozens of places, and Claude Code, the default,
must resolve to exactly the literals it used before there were profiles.

Imports nothing from rius_cc, so every module (platform_compat included)
can import it.
"""
from __future__ import annotations

import contextlib
import dataclasses
import os
from typing import Iterator, List, Tuple

FLAG = "--agent"
# A message that quotes this command to a person must name the agent: bare,
# it stores the key for Claude Code.
USE_KEY_COMMAND = "rius_ctl.sh use-key"


class UnknownAgent(ValueError):
    pass


@dataclasses.dataclass(frozen=True)
class AgentProfile:
    name: str
    display_name: str
    service_name: str
    root_name: str
    provider: str
    # Under the OS home, never an agent's own home variable: see rius_dir.
    home_parts: Tuple[str, ...]
    attr_prefix: str
    env_prefix: str
    command_prefix: str
    login_wait_budget: int
    sends_agent_on_link: bool
    # Claude Code runs a hook through a shell, so its pid is hook.py's
    # grandparent; Codex runs the hook command itself.
    hook_parent_is_agent: bool = False
    # Cursor's hooks carry no token counts.
    sends_tokens: bool = True

    def rius_dir(self, home: str) -> str:
        """Where this agent's key, settings and state live: under the OS
        home, never under CODEX_HOME. A project can set environment
        variables for the hooks and the commands, so honouring one here
        would let a cloned repo pick where the key is read from and where
        traces are switched on."""
        return os.path.join(home, *self.home_parts, "rius")

    def state_dir(self, home: str) -> str:
        return os.path.join(self.rius_dir(home), "state")

    def log_dir(self, home: str) -> str:
        return os.path.join(self.rius_dir(home), "log")

    def env_var(self, suffix: str) -> str:
        return self.env_prefix + suffix

    def command(self, action: str) -> str:
        return self.command_prefix + action

    def localize(self, text: str) -> str:
        """Messages are written in Claude Code's terms; say them in this
        agent's."""
        if self is CLAUDE_CODE:
            return text
        if not self.sends_tokens:
            text = text.replace("(models, tokens, timing)", "(models, timing)")
        return (text.replace(CLAUDE_CODE.command_prefix, self.command_prefix)
                .replace(CLAUDE_CODE.env_prefix, self.env_prefix)
                .replace(CLAUDE_CODE.display_name, self.display_name)
                .replace("files Claude reads",
                         "files %s reads" % self.display_name)
                .replace(USE_KEY_COMMAND + "`", "%s %s %s`"
                         % (USE_KEY_COMMAND, FLAG, self.name)))


# Wait budgets stay under the agent's own limit on one shell command, so a
# `login-wait` returns (and can be re-run) instead of being killed mid-poll.
CLAUDE_CODE = AgentProfile(
    name="claude-code", display_name="Claude Code",
    service_name="claude-code", root_name="claude-code session",
    provider="anthropic", home_parts=(".claude",),
    attr_prefix="cc.", env_prefix="RIUS_CLAUDE_", command_prefix="/rius:",
    login_wait_budget=540, sends_agent_on_link=False)

CODEX = AgentProfile(
    name="codex", display_name="Codex",
    service_name="codex", root_name="codex session",
    provider="openai", home_parts=(".codex",),
    attr_prefix="codex.", env_prefix="RIUS_CODEX_",
    command_prefix="$rius:rius-",
    login_wait_budget=540, sends_agent_on_link=True,
    hook_parent_is_agent=True)

# Cursor routes to several vendors, so its provider comes from each model name.
CURSOR = AgentProfile(
    name="cursor", display_name="Cursor",
    service_name="cursor", root_name="cursor session",
    provider="", home_parts=(".cursor",),
    attr_prefix="cursor.", env_prefix="RIUS_CURSOR_", command_prefix="/rius-",
    login_wait_budget=540, sends_agent_on_link=True, sends_tokens=False)

PROFILES = {p.name: p for p in (CLAUDE_CODE, CODEX, CURSOR)}

_active = CLAUDE_CODE


def active() -> AgentProfile:
    return _active


@contextlib.contextmanager
def using(profile: AgentProfile) -> Iterator[AgentProfile]:
    global _active
    previous, _active = _active, profile
    try:
        yield profile
    finally:
        _active = previous


def activate(profile: AgentProfile) -> None:
    """For a process entry point: serve `profile` until the process exits."""
    global _active
    _active = profile


def select(name: str) -> AgentProfile:
    try:
        return PROFILES[name]
    except KeyError:
        raise UnknownAgent("unknown agent %r (choose %s)"
                           % (name, ", ".join(sorted(PROFILES))))


def split_flag(argv: List[str]) -> Tuple[str, List[str]]:
    """(agent name, argv without `--agent <name>`). Anything after `--` is
    text a person typed, never this flag."""
    rest = list(argv)
    end = rest.index("--") if "--" in rest else len(rest)
    if FLAG not in rest[:end]:
        return CLAUDE_CODE.name, rest
    i = rest.index(FLAG)
    if i + 1 >= len(rest):
        raise UnknownAgent("%s needs a name" % FLAG)
    name = rest[i + 1]
    del rest[i:i + 2]
    return name, rest


def from_argv(argv: List[str]) -> Tuple[AgentProfile, List[str]]:
    name, rest = split_flag(argv)
    return select(name), rest


def child_argv(profile: AgentProfile) -> List[str]:
    """The flag that hands `profile` to a spawned script. Empty for Claude
    Code, so its children's command lines are what they always were."""
    return [] if profile.name == CLAUDE_CODE.name else [FLAG, profile.name]
