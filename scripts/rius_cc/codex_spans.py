"""Codex rollout records -> the span tree spans.py builds for Claude Code.

The same Span objects, ids, pending convention and capture gate, so otlp.py
encodes them unchanged. The tree:

    AGENT  "codex session"            session:<thread id>
      CHAIN  turn                     one per Codex turn id
        LLM  <model>                  one per token_count (one model call)
          TOOL  <tool>                function_call / custom_tool_call, by call_id
            AGENT  <agent type>       a subagent, built from its own rollout
              CHAIN  turn  ...        with a state of its own (new_subagent_state)

Token counts are OpenAI's: `input_tokens` already includes the cached ones,
which is what argus-core pricing expects, so it is sent as given with
`cached_input_tokens` as the cache-read subset. Reasoning tokens are part of
`output_tokens` and are only reported as their own split.

Everything in `state` is plain JSON, persisted between hook invocations, and
holds no content: a prompt, a reply or a tool's input is read again from the
rollout when the span it belongs to is finished (see `_hold`).
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from . import codex_rollout as cr
from . import codex_script, command_class, scrub
from .spans import (ERROR_MESSAGE_MAX_BYTES, TOOL_ERROR_WITHHELD, Ctx, Span,
                    _base_attrs, _content_attr, span_id_for, trace_id_for,
                    truncate)

PROVIDER_NAME = "openai"
DEFAULT_ROOT_NAME = "codex session"

# How Codex's exec_command reports a command's end, and where its output starts.
# Ten digits at most, as in spans._EXIT_CODE.
_EXIT_CODE = re.compile(r"^Process exited with code (-?\d{1,10})\s*$", re.MULTILINE)
_OUTPUT_MARKER = "\nOutput:\n"

# In place of the output of a call that was not checked for a secret file.
OUTPUT_NOT_CHECKED = "[redacted:not-checked]"

SPAWN_TOOL = "multi_agent_v1__spawn_agent"
# However a version namespaces it: a script calls `tools.<namespace>__spawn_agent`.
SPAWN_TOOL_SUFFIX = "spawn_agent"
SPAWN_WORD = "spawn"
# Code mode's one tool: runs a script that calls the others.
EXEC_TOOL = "exec"
# The tools whose arguments are a shell command: `cmd` as a string, or the
# older `shell` tool's `command` as a list of words.
SHELL_TOOLS = ("exec_command", "shell")
# How many exec calls an agent remembers, to find the one that spawned a
# subagent (spawning_tool).
_EXECS_KEPT = 256


def new_state() -> dict:
    return {"root_started": False, "root_start_ns": 0, "finalized": False,
            "session": {}, "model": "", "turn": None, "open_tools": {},
            "spawned": {}, "execs": []}


def upgrade_state(state: dict) -> None:
    """Bring a state saved by 0.6.0 up to this builder's. It kept the text
    of the open turn and of open tools in the state file, and the exec calls
    in `tool_starts`; none of that is left behind, so the plaintext does not
    outlive the upgrade and a subagent spawned by a call that began before it
    still finds that call."""
    open_ids = {tool["span_id"] for tool in state["open_tools"].values()}
    execs = state.setdefault("execs", [])
    for start_ns, span_id, name in state.pop("tool_starts", None) or []:
        if name == EXEC_TOOL:
            # How long a finished call ran is not known: it is not running.
            execs.append({"span_id": span_id, "start_ns": start_ns,
                                   "end_ns": 0 if span_id in open_ids else start_ns,
                                   "may_spawn": True})
    for key in ("text", "reply"):
        (state.get("turn") or {}).pop(key, None)
    for tool in state["open_tools"].values():
        tool.pop("input_json", None)
        if tool.pop("mcp_error", None) is not None:
            tool["mcp_failed"] = True


def new_subagent_state(agent_id: str, agent_type: str,
                       parent_span_id: str) -> dict:
    """State for one subagent's rollout: its root hangs under the
    spawn_agent call that started it, in the parent's trace."""
    state = new_state()
    state.update({"root_id": span_id_for("subagent:" + agent_id),
                  "root_parent": parent_span_id,
                  "root_name": agent_type or "subagent",
                  "root_attrs": {"gen_ai.agent.name": agent_type,
                                 "codex.subagent.id": agent_id}})
    return state


def _attrs(ctx: Ctx, kind_oi: str) -> Dict[str, Any]:
    attrs = _base_attrs(ctx, kind_oi)
    if "gen_ai.provider.name" in attrs:
        attrs["gen_ai.provider.name"] = PROVIDER_NAME
    return attrs


def _root_span_id(state: dict, ctx: Ctx) -> str:
    return (state.get("root_id")
            or span_id_for("session:" + ctx.conversation_id))


def _llm_span_id(turn: dict) -> str:
    return span_id_for("llm:%s:%d" % (turn["turn_id"], turn["llm_index"]))


def _pending(ctx: Ctx, span_id: str, parent: Optional[str], name: str,
             kind_oi: str, start_ns: int, extra: Dict[str, Any]) -> Span:
    attrs = _attrs(ctx, kind_oi)
    attrs.update(extra)
    attrs["glassflow.span.pending"] = True
    return Span(trace_id=trace_id_for(ctx.conversation_id), span_id=span_id,
                parent_span_id=parent, name=name, kind_oi=kind_oi,
                start_ns=start_ns, end_ns=start_ns, attributes=attrs,
                status_code="UNSET", status_message="", pending=True)


def _finished(ctx: Ctx, span_id: str, parent: Optional[str], name: str,
              kind_oi: str, start_ns: int, end_ns: int, attrs: Dict[str, Any],
              status_code: str = "OK", status_message: str = "",
              events: Optional[list] = None) -> Span:
    return Span(trace_id=trace_id_for(ctx.conversation_id), span_id=span_id,
                parent_span_id=parent, name=name, kind_oi=kind_oi,
                start_ns=start_ns, end_ns=end_ns, attributes=attrs,
                status_code=status_code, status_message=status_message,
                pending=False, events=events)


def _hold(ctx: Ctx, rec) -> Optional[list]:
    """Where `rec`'s content can be read again, for when a span is finished.
    That, not the content, is what the state keeps: it is saved in plaintext
    on every hook event, so it never holds a prompt, a reply or a tool's
    input."""
    if not ctx.capture_content or not rec.path or rec.offset < 0:
        return None
    return [rec.path, rec.offset, rec.timestamp_ns]


def _recall(ctx: Ctx, held: Optional[list], field: str) -> str:
    """What `_hold` pointed at, or nothing: with capture off the rollout is
    not read at all, and a line that is no longer there, or a place that is
    not one, gives nothing either."""
    if not held or not ctx.capture_content:
        return ""
    try:
        path, offset, timestamp_ns = held
        rec = cr.read_at(path, offset)
    except (TypeError, ValueError):
        return ""
    if rec is None or rec.timestamp_ns != timestamp_ns:
        return ""
    return rec.get(field) or ""


def uuid7_ms(value: str) -> Optional[int]:
    """The millisecond a UUIDv7 (Codex's thread and turn ids) was minted."""
    digits = (value or "").replace("-", "")
    if len(digits) != 32 or digits[12] != "7":
        return None
    try:
        return int(digits[:12], 16)
    except ValueError:
        return None


def _on_session(rec, state, ctx, out):
    thread_id = rec.get("thread_id")
    if state.get("thread_id") and thread_id != state["thread_id"]:
        return      # a fork's copy of the session it was forked from
    state["thread_id"] = thread_id
    if rec.get("forked_from_id"):
        state["forked_at_ms"] = uuid7_ms(thread_id)
    state["session"] = {k: rec.get(k, "") for k in
                        ("cli_version", "cwd", "originator", "git_branch")}


def _on_turn_context(rec, state, ctx, out):
    if rec.get("model"):
        state["model"] = rec.get("model")


def _on_turn_start(rec, state, ctx, out):
    if state["turn"] is not None:
        out += _close_turn(state, ctx, rec.timestamp_ns, None)
    turn_id = rec.get("turn_id")
    state["turn"] = {"turn_id": turn_id, "span_id": span_id_for("turn:" + turn_id),
                     "start_ns": rec.timestamp_ns, "text_at": None,
                     "reply_at": [], "llm_index": 0, "llm_start_ns": rec.timestamp_ns}
    out.append(_pending(ctx, state["turn"]["span_id"],
                        _root_span_id(state, ctx), "turn", "CHAIN",
                        rec.timestamp_ns, {}))


def _on_user_message(rec, state, ctx, out):
    turn = state["turn"]
    turn["text_at"] = _hold(ctx, rec)
    turn["llm_start_ns"] = rec.timestamp_ns


def _on_agent_message(rec, state, ctx, out):
    held = _hold(ctx, rec)
    if held:
        state["turn"].setdefault("reply_at", []).append(held)


def _usage_attrs(ctx: Ctx, model: str, rec) -> Dict[str, Any]:
    attrs = _attrs(ctx, "LLM")
    if model:
        attrs["gen_ai.request.model"] = model
        attrs["gen_ai.response.model"] = model
    attrs["gen_ai.usage.input_tokens"] = rec.get("input_tokens")
    attrs["gen_ai.usage.output_tokens"] = rec.get("output_tokens")
    attrs["gen_ai.usage.cache_read.input_tokens"] = rec.get("cached_input_tokens")
    attrs["gen_ai.usage.reasoning.output_tokens"] = rec.get("reasoning_output_tokens")
    return attrs


def _repeats_last_call(rec, state: dict) -> bool:
    """A token_count that leaves the session's running total where it was
    reports no new model call: Codex re-sends its last usage alongside
    other updates (rate limits, say)."""
    total = rec.get("session_total_tokens")
    if not total:
        return False
    if total == state.get("session_total_tokens"):
        return True
    state["session_total_tokens"] = total
    return False


def _reply(ctx: Ctx, turn: dict) -> str:
    return "".join(_recall(ctx, held, "text")
                   for held in turn.get("reply_at") or [])


def _on_usage(rec, state, ctx, out):
    if _repeats_last_call(rec, state):
        return
    turn = state["turn"]
    attrs = _usage_attrs(ctx, state["model"], rec)
    _content_attr(ctx, attrs, "output.value", _reply(ctx, turn))
    out.append(_finished(ctx, _llm_span_id(turn), turn["span_id"],
                         state["model"] or "model call", "LLM",
                         turn["llm_start_ns"], rec.timestamp_ns, attrs))
    turn["llm_index"] += 1
    turn["llm_start_ns"] = rec.timestamp_ns
    turn["reply_at"] = []


def _on_tool_call(rec, state, ctx, out):
    call_id = rec.get("call_id")
    name = rec.get("name")
    script = rec.get("arguments") if name == EXEC_TOOL else ""
    wrapped = codex_script.called_tools(script)
    tool = {"span_id": span_id_for(call_id), "parent_span_id": _llm_span_id(state["turn"]),
            "start_ns": rec.timestamp_ns, "tool_name": name,
            "span_name": _span_name(name, wrapped),
            "input_at": _hold(ctx, rec),
            "mcp_failed": False, "error_at": None,
            "command_class": _command_class(name, rec.get("arguments"), wrapped, script)}
    if ctx.capture_content:
        tool["secret_file"] = reads_secret_file(rec.get("arguments"))
    state["open_tools"][call_id] = tool
    if name == EXEC_TOOL:
        _remember_exec(state, tool, _may_spawn(script, wrapped))
    extra = {"gen_ai.tool.name": tool["tool_name"]}
    if tool["command_class"]:
        extra[command_class.ATTRIBUTE] = tool["command_class"]
    out.append(_pending(ctx, tool["span_id"], tool["parent_span_id"],
                        _name(tool), "TOOL", rec.timestamp_ns, extra))


def _command_class(name: str, arguments, wrapped: List[str],
                   script: str) -> Optional[str]:
    """The class of the command a call runs, read now and kept as one word.

    A code-mode script that calls exec_command runs commands it quotes, so
    its quoted strings are read as commands: the highest class among them.
    A quoted string that is not a command reads as "other" and changes
    nothing above it."""
    if name in SHELL_TOOLS:
        return (command_class.of_input(arguments, "cmd")
                or command_class.of_input(arguments, "command"))
    if name == EXEC_TOOL and any(t in SHELL_TOOLS for t in wrapped):
        return command_class.classify_any(codex_script.string_literals(script))
    return None


def _on_mcp_result(rec, state, ctx, out):
    tool = state["open_tools"].get(rec.get("call_id"))
    if tool is not None and rec.get("is_error"):
        tool["mcp_failed"] = True
        tool["error_at"] = _hold(ctx, rec)


def _on_subagent_started(rec, state, ctx, out):
    """multi_agent_v2: the call that started a thread is named by the event,
    not by the call's output."""
    tool = state["open_tools"].get(rec.get("call_id"))
    if tool is not None and rec.get("agent_id"):
        state.setdefault("spawned", {})[rec.get("agent_id")] = tool["span_id"]


def _command_output(output: str) -> str:
    head, marker, body = output.partition(_OUTPUT_MARKER)
    return body if marker else output


def _mcp_text(body: str) -> str:
    """The text parts of an MCP result Codex wrote out as JSON content."""
    try:
        parts = json.loads(body)
    except (ValueError, RecursionError):
        return body
    if not isinstance(parts, list):
        return body
    return "\n".join(p.get("text", "") for p in parts if isinstance(p, dict))


def exit_code_of(output) -> Optional[int]:
    """The code exec_command says its command exited with, None when the
    output says none (an MCP call, a command still running)."""
    if not isinstance(output, str):
        return None
    match = _EXIT_CODE.search(output.partition(_OUTPUT_MARKER)[0])
    return int(match.group(1)) if match else None


def tool_error(tool: dict, output: str) -> Optional[str]:
    """error.type for a failed call, None for one that succeeded.

    A command that exited non-zero is `<tool>.exit_<code>`, as spans.py types
    a Claude Code Bash failure; an MCP call its server marked as an error is
    `<tool>.tool_error`. Anything else reads as success: Codex has no generic
    error flag on a tool's output.
    """
    code = exit_code_of(output)
    if code:
        return "%s.exit_%d" % (tool["tool_name"], code)
    if tool.get("mcp_failed"):
        return tool["tool_name"] + ".tool_error"
    return None


def _error_detail(ctx: Ctx, tool: dict, output: str) -> str:
    error = _recall(ctx, tool.get("error_at"), "error")
    if error:
        return error
    body = _command_output(output)
    return _mcp_text(body) if tool.get("mcp_failed") else body


def error_line(text: str) -> str:
    """The last line of `text`, secrets removed, capped."""
    lines = [line.strip() for line in scrub.scrub(text).splitlines()
             if line.strip()]
    return truncate(lines[-1], ERROR_MESSAGE_MAX_BYTES) if lines else ""


def reads_secret_file(arguments: str) -> bool:
    """scrub.reads_secret_file for Codex's shell tools too: exec_command
    takes `cmd`, the older shell tool an argv list as `command`, and code
    mode's `exec` a script."""
    try:
        if scrub.reads_secret_file(arguments):
            return True
        args = json.loads(arguments or "{}")
    except ValueError:
        return _script_reads_secret_file(arguments)
    except RecursionError:
        return True     # nested too deep to read: withhold
    if not isinstance(args, dict):
        return False
    command = args.get("cmd")
    if isinstance(args.get("command"), list):
        command = " ".join(str(word) for word in args["command"])
    return isinstance(command, str) and scrub.reads_secret_file(
        json.dumps({"command": command}))


# What also ends a word of a script: the punctuation of its objects and arrays.
_SCRIPT_PUNCTUATION = re.compile(r"[,{}\[\]:]")


def _script_reads_secret_file(script: str) -> bool:
    """Code mode's `exec` runs a script, such as
    `tools.exec_command({cmd: "cat .env"})`, not JSON arguments. It reads a
    secret file when a secret-shaped name (`.env*`, `*.pem`, `*.key`, ...)
    stands anywhere in its text, as a path or as a word of a command: this
    does not rest on the scanner, which is only a best effort, so it also
    counts a property (`obj.key`) or a comment that names one. The literals the
    scanner found are checked too. A script too long to read is taken to."""
    if len(script) > codex_script.MAX_CHARS:
        return True
    literals = codex_script.string_literals(script)
    return any(scrub.reads_secret_file(json.dumps({"command": text}))
               for text in (_SCRIPT_PUNCTUATION.sub(" ", script), " ".join(literals)))


_EXEC_NAME_TOOLS = 3


def _span_name(tool_name: str, wrapped: List[str]) -> str:
    """Every code-mode call is `exec`, so it is named after the tools its
    script calls instead; `exec` stays for one that calls none."""
    if not wrapped:
        return tool_name
    extra = len(wrapped) - _EXEC_NAME_TOOLS
    names = ", ".join(wrapped[:_EXEC_NAME_TOOLS])
    return scrub.scrub(names) + (" +%d" % extra if extra > 0 else "")


def _name(tool: dict) -> str:
    return tool.get("span_name") or tool["tool_name"]


def _shown(tool: dict, output: str) -> str:
    """A call's output, or what stands for it when the call read a secret
    file, or when it is not known whether it did (it began with content off,
    or before an upgrade): then nothing of it may be sent."""
    verdict = tool.get("secret_file")
    if verdict is None:
        return OUTPUT_NOT_CHECKED
    return scrub.SECRET_FILE_MARKER if verdict else output


def _arguments(ctx: Ctx, tool: dict) -> str:
    return _recall(ctx, tool.get("input_at"), "arguments")


def _on_tool_output(rec, state, ctx, out):
    tool = state["open_tools"].pop(rec.get("call_id"), None)
    if state["turn"] is not None:
        state["turn"]["llm_start_ns"] = rec.timestamp_ns
    if tool is None:
        return      # called before this session was traced
    _end_exec(state, tool, rec.timestamp_ns)
    output = rec.get("output")
    if tool["tool_name"] == SPAWN_TOOL:
        _note_spawned(state, tool, output)
    attrs = _attrs(ctx, "TOOL")
    attrs["gen_ai.tool.name"] = tool["tool_name"]
    if tool.get("command_class"):
        attrs[command_class.ATTRIBUTE] = tool["command_class"]
        code = exit_code_of(output)
        if code is not None:
            attrs[command_class.EXIT_CODE_ATTRIBUTE] = code
    arguments = _arguments(ctx, tool)
    # Arguments are JSON, and an output often is (JSON.stringify in exec).
    _content_attr(ctx, attrs, "input.value", arguments, json_text=True)
    withheld = tool.get("secret_file") is not False
    _content_attr(ctx, attrs, "output.value", _shown(tool, output), json_text=True)
    error_type = tool_error(tool, output)
    status_message, events = "", []
    if error_type:
        attrs["error.type"] = error_type
        if not ctx.capture_content:
            status_message = TOOL_ERROR_WITHHELD
        elif withheld:
            status_message = error_type
        else:
            status_message = error_line(_error_detail(ctx, tool, output)) or error_type
        events.append((rec.timestamp_ns, "exception", {
            "exception.type": error_type, "exception.message": status_message}))
    out.append(_finished(ctx, tool["span_id"], tool["parent_span_id"],
                         _name(tool), "TOOL", tool["start_ns"],
                         rec.timestamp_ns, attrs,
                         "ERROR" if error_type else "OK", status_message, events))


def _may_spawn(script: str, wrapped: List[str]) -> bool:
    """Whether an `exec` script could spawn an agent. Decided on the whole
    text, not on the names found in it: a spawn inside a template literal or
    called as `tools["..."]` is not among them. One that names no tool at all
    (it calls them through an alias, say) might have."""
    return not wrapped or SPAWN_WORD in script.lower()


def _remember_exec(state: dict, tool: dict, may_spawn: bool) -> None:
    execs = state.setdefault("execs", [])
    execs.append({"span_id": tool["span_id"], "start_ns": tool["start_ns"],
                  "end_ns": 0, "may_spawn": may_spawn})
    del execs[:-_EXECS_KEPT]


def _end_exec(state: dict, tool: dict, end_ns: int) -> None:
    if tool["tool_name"] != EXEC_TOOL:
        return
    for call in state.get("execs") or []:
        if call["span_id"] == tool["span_id"]:
            call["end_ns"] = end_ns


def _awaits_spawn_output(state: dict) -> bool:
    """A spawn_agent call is open: what it says, or what is written beside
    it (multi_agent_v2's sub_agent_activity), names its agent exactly, and a
    guess made now would never be corrected."""
    return any(tool["tool_name"].endswith(SPAWN_TOOL_SUFFIX)
               for tool in state["open_tools"].values())


def spawning_tool(state: dict, created_ns: int,
                  closing: bool = False) -> Optional[str]:
    """The span id of the `exec` call that spawned a subagent created at
    `created_ns`, or None while a spawn_agent call is still open (unless the
    trace is `closing`: nothing more will say). In code
    mode (`code_mode_host`, on by default) a spawn runs inside an `exec` call
    whose output need not name the agent, so what is left to go by is when
    the agent was created and what the scripts say. The calls that were
    running then and may spawn come first, then any that was running, then
    the last begun, so a subagent always has a parent; of those, the
    earliest that has spawned nothing yet: calls running at the same time
    each get their own subagent, in the order they began, however late the
    others began."""
    if not closing and _awaits_spawn_output(state):
        return None
    begun = [call for call in state.get("execs") or []
             if call["start_ns"] <= created_ns]
    running = [call for call in begun
               if not call["end_ns"] or call["end_ns"] >= created_ns]
    candidates = (_spawners(running) or running or _spawners(begun)[-1:]
                  or begun[-1:])
    held = set((state.get("spawned") or {}).values())
    free = [call for call in candidates if call["span_id"] not in held]
    chosen = (free or candidates)[:1]
    return chosen[0]["span_id"] if chosen else None


def adopting_span(state: dict, ctx: Ctx) -> str:
    """The span a subagent that no call can be found for hangs under when
    the trace is closing: its spawner's open turn, else its root."""
    turn = state.get("turn")
    return turn["span_id"] if turn else _root_span_id(state, ctx)


def _spawners(calls: List[dict]) -> List[dict]:
    return [call for call in calls if call["may_spawn"]]


def _note_spawned(state: dict, tool: dict, output: str) -> None:
    """spawn_agent answers with the new agent's id, which names its rollout."""
    try:
        agent_id = json.loads(output).get("agent_id")
    except (ValueError, AttributeError, RecursionError):
        return
    if isinstance(agent_id, str) and agent_id:
        state.setdefault("spawned", {})[agent_id] = tool["span_id"]


def _close_open_tools(state: dict, ctx: Ctx, now_ns: int) -> List[Span]:
    """A call whose output never came (interrupted) still needs a finished
    row: the backend counts a trace only when every span has one."""
    out = []
    for tool in state["open_tools"].values():
        _end_exec(state, tool, now_ns)
        attrs = _attrs(ctx, "TOOL")
        attrs["gen_ai.tool.name"] = tool["tool_name"]
        _content_attr(ctx, attrs, "input.value", _arguments(ctx, tool),
                      json_text=True)
        out.append(_finished(ctx, tool["span_id"], tool["parent_span_id"],
                             _name(tool), "TOOL", tool["start_ns"],
                             now_ns, attrs, status_code="UNSET"))
    state["open_tools"] = {}
    return out


def _close_turn(state: dict, ctx: Ctx, end_ns: int, rec) -> List[Span]:
    turn = state["turn"]
    out = _close_open_tools(state, ctx, end_ns)
    attrs = _attrs(ctx, "CHAIN")
    attrs["codex.turn.id"] = turn["turn_id"]
    if rec is not None:
        if rec.get("duration_ms"):
            attrs["codex.turn.duration_ms"] = rec.get("duration_ms")
        if rec.get("ttft_ms"):
            attrs["codex.turn.ttft_ms"] = rec.get("ttft_ms")
        if rec.get("aborted"):
            attrs["codex.turn.aborted"] = rec.get("reason") or "aborted"
        _content_attr(ctx, attrs, "output.value", rec.get("last_agent_message"))
    _content_attr(ctx, attrs, "input.value",
                  _recall(ctx, turn.get("text_at"), "text"))
    out.append(_finished(ctx, turn["span_id"], _root_span_id(state, ctx),
                         "turn", "CHAIN", turn["start_ns"], end_ns, attrs))
    state["turn"] = None
    return out


def _on_turn_end(rec, state, ctx, out):
    out += _close_turn(state, ctx, rec.timestamp_ns, rec)


_HANDLERS = {
    cr.SESSION: _on_session,
    cr.TURN_CONTEXT: _on_turn_context,
    cr.TURN_START: _on_turn_start,
    cr.USER_MESSAGE: _on_user_message,
    cr.AGENT_MESSAGE: _on_agent_message,
    cr.USAGE: _on_usage,
    cr.TOOL_CALL: _on_tool_call,
    cr.MCP_RESULT: _on_mcp_result,
    cr.SUBAGENT_STARTED: _on_subagent_started,
    cr.TOOL_OUTPUT: _on_tool_output,
    cr.TURN_END: _on_turn_end,
}

# Kinds that only mean something inside a turn. One seen with no turn open
# began before this session was traced, and is dropped.
_NEEDS_TURN = {cr.USER_MESSAGE, cr.AGENT_MESSAGE, cr.USAGE, cr.TOOL_CALL,
               cr.TURN_END}


def _inherited(rec, state: dict) -> bool:
    """A turn a forked rollout copied from the agent it was forked from
    (a subagent spawned with fork_context, say). Its parent's trace already
    has it: replayed, its model calls would be counted twice. Codex ids are
    UUIDv7, so a turn begun before this thread existed is an inherited one."""
    forked_at = state.get("forked_at_ms")
    started = uuid7_ms(rec.get("turn_id"))
    return bool(forked_at) and started is not None and started < forked_at


def _root_attrs(state: dict) -> Dict[str, Any]:
    return {k: v for k, v in (state.get("root_attrs") or {}).items() if v}


def _root(state: dict, ctx: Ctx, start_ns: int) -> Span:
    return _pending(ctx, _root_span_id(state, ctx), state.get("root_parent"),
                    state.get("root_name") or DEFAULT_ROOT_NAME, "AGENT",
                    start_ns, _root_attrs(state))


def open_root(state: dict, ctx: Ctx) -> Span:
    """The root's pending row again, as first sent but for its name."""
    return _root(state, ctx, state["root_start_ns"])


def _adopt_role(state: dict, records: List[Any]) -> None:
    """A subagent's rollout names its role, which its hooks may not have
    told us before its root span goes out."""
    if not state.get("root_id"):
        return
    session = next((rec for rec in records if rec.kind == cr.SESSION), None)
    if session is not None and session.get("agent_role"):
        state["root_name"] = session.get("agent_role")
        state["root_attrs"]["gen_ai.agent.name"] = session.get("agent_role")


def build(records: List[Any], state: dict, ctx: Ctx) -> List[Span]:
    """Records read since the last call -> the spans they open or finish."""
    out: List[Span] = []
    if records:
        state["finalized"] = False
    if records and not state["root_started"]:
        _adopt_role(state, records)
        state["root_started"] = True
        state["root_start_ns"] = records[0].timestamp_ns
        out.append(_root(state, ctx, records[0].timestamp_ns))
    for rec in records:
        if rec.kind in _NEEDS_TURN and state["turn"] is None:
            continue
        if rec.kind == cr.TURN_START and _inherited(rec, state):
            continue    # and with no turn open, all that follows it too
        _HANDLERS[rec.kind](rec, state, ctx, out)
    return out


def finalize_session(state: dict, ctx: Ctx, now_ns: int) -> List[Span]:
    """Close everything still open, so no span of the trace stays pending."""
    out: List[Span] = []
    if state["turn"] is not None:
        out += _close_turn(state, ctx, now_ns, None)
    state["finalized"] = True
    if not state["root_started"]:
        return out
    attrs = _attrs(ctx, "AGENT")
    attrs.update(_root_attrs(state))
    out.append(_finished(ctx, _root_span_id(state, ctx),
                         state.get("root_parent"),
                         state.get("root_name") or DEFAULT_ROOT_NAME,
                         "AGENT", state["root_start_ns"] or now_ns, now_ns,
                         attrs))
    return out


def resource_attributes(state: dict) -> Dict[str, str]:
    """The session's codex.* resource attributes, beside service.name."""
    session = state.get("session") or {}
    return {
        "codex.version": session.get("cli_version", ""),
        "codex.cwd": session.get("cwd", ""),
        "codex.git_branch": session.get("git_branch", ""),
        "codex.originator": session.get("originator", ""),
    }
