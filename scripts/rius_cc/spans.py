"""Transcript entries -> OTLP GenAI span tree.

Knows nothing about JSONL parsing (that's transcript.py) or OTLP encoding
(that's a later task). This module only decides what spans exist, their
ids, their nesting, and their attributes.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional

PROVIDER_NAME = "anthropic"

# The tool that spawns a subagent. It is "Agent" in Claude Code 2.1.x and was
# "Task" before that; matching only one of them means every subagent in that
# version is invisible, which is exactly what happened. Both, always.
SUBAGENT_TOOL_NAMES = ("Agent", "Task")

# Status.message for a failed tool when content capture is off. Spelled out
# rather than a bare "tool error" so a viewer can tell "we deliberately did
# not send you the detail" from "the detail went missing".
TOOL_ERROR_WITHHELD = "tool error (detail withheld: RIUS_CAPTURE_CONTENT=false)"

# A "user" entry whose text opens with one of these was injected by the
# harness, not typed by the user: a slash-command caveat, a background-task
# notification, a subagent's report, a bash-mode command. They are real work
# and keep their turn span, but the span says where the text came from.
SYSTEM_TURN_PREFIXES = (
    "<local-command-caveat",
    "<local-command-stdout",
    "<command-name",
    "<command-message",
    "<task-notification",
    "<agent-message",
    "<bash-input",
    "<bash-stdout",
    "<bash-stderr",
    "<system-reminder",
    "<user-prompt-submit-hook",
)

_OPERATION_BY_KIND = {
    "AGENT": "invoke_agent",
    "LLM": "chat",
    "TOOL": "execute_tool",
    "CHAIN": None,
}

# Allowlist: the ONLY attribute keys a pending span may carry, plus any key
# prefixed with "gen_ai.request.". This is deliberately an allowlist, not a
# blocklist -- a pending span is emitted before work finishes and must never
# leak content such as input.value / output.value.
_PENDING_ALLOWED_KEYS = {
    "openinference.span.kind",
    "gen_ai.operation.name",
    "gen_ai.provider.name",
    "gen_ai.tool.name",
    "session.id",
    "glassflow.span.pending",
    # Identity, not content: which named agent this is and where a turn's
    # text came from. The UI filters on these while the span is still
    # running, and neither can carry a prompt or a tool's output.
    "gen_ai.agent.name",
    "cc.turn.source",
    "cc.subagent.id",
    "cc.subagent.depth",
}


def _filter_pending_attrs(attributes: Dict[str, Any]) -> Dict[str, Any]:
    out = {}
    for k, v in attributes.items():
        if k in _PENDING_ALLOWED_KEYS or k.startswith("gen_ai.request."):
            out[k] = v
    return out


def trace_id_for(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]


def span_id_for(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def truncate(value: str, limit: int) -> str:
    """Cap a value at `limit` BYTES, with an explicit marker for the rest.

    Slice the encoded bytes, not the characters: measuring bytes and slicing
    characters let a non-ASCII value come out up to 4x over the cap, and made
    the reported byte count wrong. errors="ignore" drops a codepoint the cut
    landed in the middle of, rather than emitting a replacement character.
    """
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value
    dropped = len(encoded) - limit
    head = encoded[:limit].decode("utf-8", errors="ignore")
    return head + " …[truncated %d bytes]" % dropped


class Ctx:
    """Plain, mutable context. Tests mutate `capture_content` post-construction."""

    def __init__(self, session_id, cwd, git_branch, cc_version, service_name,
                 capture_content, max_attr_bytes):
        self.session_id = session_id
        self.cwd = cwd
        self.git_branch = git_branch
        self.cc_version = cc_version
        self.service_name = service_name
        self.capture_content = capture_content
        self.max_attr_bytes = max_attr_bytes


class Span:
    def __init__(self, trace_id, span_id, parent_span_id, name, kind_oi,
                 start_ns, end_ns, attributes, status_code, status_message,
                 pending):
        self.trace_id = trace_id
        self.span_id = span_id
        self.parent_span_id = parent_span_id
        self.name = name
        self.kind_oi = kind_oi
        self.start_ns = start_ns
        self.end_ns = end_ns
        self.attributes = _filter_pending_attrs(attributes) if pending else dict(attributes)
        self.status_code = status_code
        self.status_message = status_message
        self.pending = pending


def _base_attrs(ctx: Ctx, kind_oi: str) -> Dict[str, Any]:
    attrs: Dict[str, Any] = {
        "openinference.span.kind": kind_oi,
        "session.id": ctx.session_id,
    }
    op = _OPERATION_BY_KIND.get(kind_oi)
    if op is not None:
        attrs["gen_ai.operation.name"] = op
    if kind_oi in ("LLM", "TOOL", "AGENT"):
        attrs["gen_ai.provider.name"] = PROVIDER_NAME
    return attrs


def _content_attr(ctx: Ctx, attrs: Dict[str, Any], key: str, value: str) -> None:
    if not ctx.capture_content:
        return
    attrs[key] = truncate(value, ctx.max_attr_bytes)


def _current_turn_parent(state: dict, root_span_id: str) -> str:
    open_turns = state["open_turns"]
    if open_turns:
        last_key = next(reversed(open_turns))
        return open_turns[last_key]["span_id"]
    return root_span_id


def turn_source_for(text: str) -> str:
    """"user" or "system" for a turn's opening text.

    Anchored at the start, never a substring search: a prompt that merely
    mentions <bash-input> is still something the user typed.
    """
    stripped = (text or "").lstrip()
    for prefix in SYSTEM_TURN_PREFIXES:
        if stripped.startswith(prefix):
            return "system"
    return "user"


def new_scope() -> dict:
    """The per-transcript bookkeeping build() mutates.

    The main session's scope IS the session state dict; a subagent gets one
    of these, kept under state["sub_scopes"][agent_id]. Plain JSON types
    only -- all of it is persisted between hook invocations.
    """
    return {"open_tools": {}, "open_turns": {}, "open_task_spans": [],
            "last_ns": 0, "started": False, "start_ns": 0, "open_gen": None}


def _generation_for(entry: Any, scope: dict, root_span_id: str,
                    inline_sidechains: bool) -> dict:
    """The generation (one API response) this assistant line belongs to.

    Claude Code writes ONE response as one line per content block, and every
    line repeats the response's usage. The lines are contiguous apart from
    the tool_results of tools that already ran, so the response in progress
    is the only one that can still grow. Its id, parent and start are fixed
    by its first line and persisted: a later hook re-emits the span, and the
    backend collapses the copies only if the span id and start match.
    Nothing here is content; the text is re-read from the file when needed.
    """
    key = entry.generation_key()
    gen = scope.get("open_gen")
    if isinstance(gen, dict) and gen.get("key") == key:
        return gen
    if inline_sidechains and entry.is_sidechain and scope["open_task_spans"]:
        # Only the main transcript: a Claude Code version that wrote
        # sidechain entries inline still nests them under the tool call.
        # Inside a subagent's own file every entry is a sidechain entry and
        # belongs to that subagent, not to the nested tool call it follows.
        parent_span_id = scope["open_task_spans"][-1]
    else:
        parent_span_id = _current_turn_parent(scope, root_span_id)
    gen = {
        "key": key,
        "span_id": span_id_for(key),
        "parent_span_id": parent_span_id,
        "start_ns": scope["last_ns"] or entry.timestamp_ns,
        "offset": entry.offset,
    }
    scope["open_gen"] = gen
    return gen


def _earlier_text(ctx: Ctx, source_path: str, gen: dict,
                  lines: List[Any]) -> str:
    """Text from lines of this response that an earlier hook already read."""
    if not ctx.capture_content or not source_path:
        return ""
    start = gen.get("offset", -1)
    end = lines[0].offset
    if start is None or start < 0 or end <= start:
        return ""
    from . import transcript
    return "".join(e.text() for e in transcript.read_between(source_path, start, end)
                   if e.kind == "assistant" and e.generation_key() == gen["key"])


def _latest(lines: List[Any], key: str) -> Any:
    for line in reversed(lines):
        value = line.message.get(key)
        if value:
            return value
    return None


def _input_tokens_inclusive(usage: Dict[str, Any]) -> int:
    """The whole prompt, cached or not.

    Anthropic's `input_tokens` counts only the uncached part (2 tokens next
    to 50k cached is normal). The OTel GenAI conventions, the Rius attribute
    reference and the Rius SDKs all send the inclusive total, with the cache
    counts as subsets of it, and the backend adds input and output to get a
    span's tokens. Sent raw, every cached token fell out of that total.
    """
    total = 0
    for key in ("input_tokens", "cache_read_input_tokens",
                "cache_creation_input_tokens"):
        value = usage.get(key)
        if isinstance(value, int):
            total += value
    return total


def _generation_span(ctx: Ctx, trace_id: str, gen: dict, lines: List[Any],
                     earlier_text: str) -> Span:
    """The LLM span for one response, from the lines of it seen so far.

    Usage comes from the LATEST line: in the main transcript every line has
    the same final usage, but a streamed subagent line can carry a partial
    count (output_tokens=1, stop_reason=None) that only its last line fixes.
    """
    model = _latest(lines, "model") or ""
    usage = _latest(lines, "usage") or {}
    stop_reason = _latest(lines, "stop_reason")

    attrs = _base_attrs(ctx, "LLM")
    if model:
        attrs["gen_ai.request.model"] = model
        attrs["gen_ai.response.model"] = model
    if "input_tokens" in usage:
        attrs["gen_ai.usage.input_tokens"] = _input_tokens_inclusive(usage)
    if "output_tokens" in usage:
        attrs["gen_ai.usage.output_tokens"] = usage["output_tokens"]
    thinking = (usage.get("output_tokens_details") or {}).get("thinking_tokens")
    if isinstance(thinking, int):
        attrs["gen_ai.usage.reasoning.output_tokens"] = thinking
    if "cache_read_input_tokens" in usage:
        attrs["gen_ai.usage.cache_read.input_tokens"] = usage["cache_read_input_tokens"]
    if "cache_creation_input_tokens" in usage:
        # Upstream OTel GenAI semconv renamed this attribute to
        # gen_ai.usage.cache_write.input_tokens (semantic-
        # conventions-genai#440). We emit BOTH keys with the same
        # value, deliberately, not belt-and-braces: a Rius backend
        # at migration 000011 (before 000013_spans_cache_write_
        # rename) reads only the old key, and dropping it would
        # silently zero its cache-write count -- this project's
        # signature failure mode. A backend at 000013+ prefers the
        # new key, so emitting it too means we stop depending on a
        # compatibility fallback that will eventually be removed.
        # Revisit and drop the legacy key once every deployment is
        # known to be at 000013+.
        attrs["gen_ai.usage.cache_write.input_tokens"] = usage["cache_creation_input_tokens"]
        attrs["gen_ai.usage.cache_creation.input_tokens"] = usage["cache_creation_input_tokens"]
    if stop_reason:
        attrs["gen_ai.response.finish_reasons"] = [stop_reason]
    _content_attr(ctx, attrs, "output.value",
                  earlier_text + "".join(line.text() for line in lines))

    return Span(
        trace_id=trace_id, span_id=gen["span_id"],
        parent_span_id=gen["parent_span_id"], name=model or "assistant",
        kind_oi="LLM", start_ns=gen["start_ns"], end_ns=lines[-1].timestamp_ns,
        attributes=attrs, status_code="OK", status_message="", pending=False,
    )


def emit_entries(entries: List[Any], scope: dict, ctx: Ctx, trace_id: str,
                 root_span_id: str, links: dict, depth: int = 0,
                 key_prefix: str = "", make_turns: bool = True,
                 inline_sidechains: bool = True,
                 source_path: str = "") -> List[Any]:
    """Entries of ONE transcript -> spans, parented under `root_span_id`.

    Used for both the main transcript (scope == the session state, root ==
    the session span) and each subagent transcript (scope == that agent's
    scope, root == that agent's AGENT span). `links` collects every
    subagent-spawning tool_use seen, so subagents.expand() can find the
    transcript each one wrote -- including the ones a subagent spawns.

    `source_path` is the file `entries` came from. It is only read to recover
    the text of a response whose earlier lines an earlier hook consumed.
    """
    out: List[Span] = []
    # One generation span per API response in this batch, emitted after the
    # loop so it carries every line of the response that has arrived.
    batch_gens: Dict[str, Any] = {}
    for entry in entries:
        if entry.kind == "user":
            tool_results = entry.tool_results()
            if tool_results:
                for tr in tool_results:
                    tool_use_id = tr.get("tool_use_id")
                    link = links.get(tool_use_id)
                    if link is not None and link.get("end_ns") is None:
                        # The tool_result is the only record that the subagent
                        # finished; its own transcript has no closing entry.
                        link["end_ns"] = entry.timestamp_ns
                    open_tool = scope["open_tools"].pop(tool_use_id, None)
                    if open_tool is None:
                        continue  # tool started before instrumentation was enabled
                    if open_tool["span_id"] in scope["open_task_spans"]:
                        scope["open_task_spans"].remove(open_tool["span_id"])
                    is_error = bool(tr.get("is_error"))
                    content = tr.get("content")
                    content_str = content if isinstance(content, str) else json.dumps(content)
                    attrs = _base_attrs(ctx, "TOOL")
                    attrs["gen_ai.tool.name"] = open_tool["tool_name"]
                    _content_attr(ctx, attrs, "input.value", open_tool["input_json"])
                    _content_attr(ctx, attrs, "output.value", content_str)
                    status_code = "ERROR" if is_error else "OK"
                    # Status.message is content too: for a failed Bash call
                    # it is the command's stdout+stderr. It must honour the
                    # capture gate exactly like input.value/output.value do,
                    # or RIUS_CAPTURE_CONTENT=false is not the guarantee the
                    # README makes it out to be.
                    if not is_error:
                        status_message = ""
                    elif ctx.capture_content:
                        status_message = truncate(content_str, ctx.max_attr_bytes)
                    else:
                        status_message = TOOL_ERROR_WITHHELD
                    out.append(Span(
                        trace_id=trace_id, span_id=open_tool["span_id"],
                        parent_span_id=open_tool["parent_span_id"], name=open_tool["tool_name"],
                        kind_oi="TOOL", start_ns=open_tool["start_ns"], end_ns=entry.timestamp_ns,
                        attributes=attrs, status_code=status_code, status_message=status_message,
                        pending=False,
                    ))
            elif make_turns:
                if entry.prompt_id and entry.prompt_id not in scope["open_turns"]:
                    # Namespaced by scope: a subagent transcript carries the
                    # PARENT's promptId, so "turn:" + promptId alone would
                    # collide with the main session's turn span.
                    turn_span_id = span_id_for(key_prefix + "turn:" + entry.prompt_id)
                    source = turn_source_for(entry.text())
                    scope["open_turns"][entry.prompt_id] = {
                        "span_id": turn_span_id,
                        "parent_span_id": root_span_id,
                        "start_ns": entry.timestamp_ns,
                        "source": source,
                        # state.save writes this dict to
                        # ~/.claude/rius/state/<sid>.json in plaintext, so
                        # keeping the prompt here would persist it to disk
                        # even with capture off. It is only ever read back to
                        # fill input.value, which the gate drops anyway.
                        "text": entry.text() if ctx.capture_content else "",
                    }
                    attrs = _base_attrs(ctx, "CHAIN")
                    attrs["cc.turn.source"] = source
                    attrs["glassflow.span.pending"] = True
                    out.append(Span(
                        trace_id=trace_id, span_id=turn_span_id, parent_span_id=root_span_id,
                        name="turn", kind_oi="CHAIN", start_ns=entry.timestamp_ns,
                        end_ns=entry.timestamp_ns, attributes=attrs, status_code="UNSET",
                        status_message="", pending=True,
                    ))

        elif entry.kind == "assistant":
            gen = _generation_for(entry, scope, root_span_id, inline_sidechains)
            batch_lines = batch_gens.setdefault(gen["key"], (gen, []))[1]
            batch_lines.append(entry)
            llm_span_id = gen["span_id"]

            for block in entry.tool_uses():
                tool_id = block.get("id")
                tool_name = block.get("name") or ""
                tool_span_id = span_id_for(tool_id)
                input_json = json.dumps(block.get("input") or {})
                scope["open_tools"][tool_id] = {
                    "span_id": tool_span_id,
                    "parent_span_id": llm_span_id,
                    "start_ns": entry.timestamp_ns,
                    "tool_name": tool_name,
                    # Same hazard as open_turns["text"] below: state.save
                    # writes this dict to ~/.claude/rius/state/<sid>.json in
                    # plaintext on every hook event, and an open tool sits
                    # here for its whole run. A tool's input is content -- a
                    # Bash command line, a Write body, and for an Agent call
                    # the subagent's entire brief. It is only ever read back
                    # to fill input.value, which the gate drops anyway.
                    "input_json": input_json if ctx.capture_content else "",
                }
                if tool_name in SUBAGENT_TOOL_NAMES:
                    scope["open_task_spans"].append(tool_span_id)
                    if tool_id and tool_id not in links:
                        # The subagent's own transcript is a separate file,
                        # found later by matching this tool_use id against
                        # subagents/*.meta.json. Recorded even if that file
                        # does not exist yet: it is written as the subagent
                        # runs, long after this tool_use appears.
                        links[tool_id] = {
                            "span_id": tool_span_id,
                            "start_ns": entry.timestamp_ns,
                            "end_ns": None,
                            "depth": depth + 1,
                            "agent_id": None,
                            "closed": False,
                        }

                attrs = _base_attrs(ctx, "TOOL")
                attrs["gen_ai.tool.name"] = tool_name
                attrs["glassflow.span.pending"] = True
                out.append(Span(
                    trace_id=trace_id, span_id=tool_span_id, parent_span_id=llm_span_id,
                    name=tool_name, kind_oi="TOOL", start_ns=entry.timestamp_ns,
                    end_ns=entry.timestamp_ns, attributes=attrs, status_code="UNSET",
                    status_message="", pending=True,
                ))

        scope["last_ns"] = entry.timestamp_ns

    for gen, lines in batch_gens.values():
        earlier = _earlier_text(ctx, source_path, gen, lines)
        out.append(_generation_span(ctx, trace_id, gen, lines, earlier))
    return out


def build(entries: List[Any], state: dict, ctx: Ctx,
          source_path: str = "") -> List[Any]:
    """The MAIN transcript. Subagent transcripts are separate files; see
    subagents.expand(), which feeds them through emit_entries() too."""
    out: List[Span] = []
    trace_id = trace_id_for(ctx.session_id)
    root_span_id = span_id_for("session:" + ctx.session_id)
    if "sub_links" not in state:
        state["sub_links"] = {}

    if entries:
        # A resumed session carries on after an earlier SessionEnd closed it.
        state["finalized"] = False
    if entries and not state.get("root_started"):
        state["root_started"] = True
        state["root_start_ns"] = entries[0].timestamp_ns
        attrs = _base_attrs(ctx, "AGENT")
        attrs["glassflow.span.pending"] = True
        out.append(Span(
            trace_id=trace_id, span_id=root_span_id, parent_span_id=None,
            name="claude-code session", kind_oi="AGENT",
            start_ns=entries[0].timestamp_ns, end_ns=entries[0].timestamp_ns,
            attributes=attrs, status_code="UNSET", status_message="",
            pending=True,
        ))

    out += emit_entries(entries, state, ctx, trace_id, root_span_id,
                        state["sub_links"], depth=0, key_prefix="",
                        make_turns=True, inline_sidechains=True,
                        source_path=source_path)
    return out


def subagent_span(ctx: Ctx, trace_id: str, span_id: str, parent_span_id: str,
                  meta: Dict[str, Any], agent_id: str, depth: int,
                  start_ns: int, end_ns: int, prompt: str,
                  pending: bool) -> Span:
    """The AGENT span for one subagent run, stamped from its meta.json.

    `gen_ai.agent.name` is the key the Rius backend's sink filters on:
    it is what makes a subagent addressable as a
    named agent in the UI instead of an anonymous span. The description and
    the prompt are content and go through the capture gate; the agent's
    name, model and depth are identity and do not.
    """
    attrs = _base_attrs(ctx, "AGENT")
    agent_type = meta.get("agentType") or ""
    if agent_type:
        attrs["gen_ai.agent.name"] = agent_type
    model = meta.get("model") or ""
    if model:
        attrs["gen_ai.request.model"] = model
    if agent_id:
        attrs["cc.subagent.id"] = agent_id
    attrs["cc.subagent.depth"] = depth
    description = meta.get("description") or ""
    if description:
        _content_attr(ctx, attrs, "gen_ai.agent.description", description)
    if prompt:
        _content_attr(ctx, attrs, "input.value", prompt)
    if pending:
        attrs["glassflow.span.pending"] = True
    return Span(
        trace_id=trace_id, span_id=span_id, parent_span_id=parent_span_id,
        name=agent_type or "subagent", kind_oi="AGENT", start_ns=start_ns,
        end_ns=end_ns, attributes=attrs,
        status_code="UNSET" if pending else "OK", status_message="",
        pending=pending,
    )


def finalize_turn(state: dict, ctx: Ctx, now_ns: int) -> List[Any]:
    out: List[Span] = []
    trace_id = trace_id_for(ctx.session_id)
    for prompt_id, turn in list(state["open_turns"].items()):
        attrs = _base_attrs(ctx, "CHAIN")
        attrs["cc.turn.source"] = turn.get("source") or "user"
        _content_attr(ctx, attrs, "input.value", turn.get("text", ""))
        out.append(Span(
            trace_id=trace_id, span_id=turn["span_id"], parent_span_id=turn["parent_span_id"],
            name="turn", kind_oi="CHAIN", start_ns=turn["start_ns"], end_ns=now_ns,
            attributes=attrs, status_code="OK", status_message="", pending=False,
        ))
    state["open_turns"] = {}
    return out


def _close_open_tools(scope: dict, ctx: Ctx, trace_id: str,
                      now_ns: int) -> List[Any]:
    """A tool whose result never arrived (interrupted, or the session ended
    mid-call) must still get a finished row: the backend counts a trace only
    when every one of its spans has one."""
    out: List[Span] = []
    for tool in scope["open_tools"].values():
        attrs = _base_attrs(ctx, "TOOL")
        attrs["gen_ai.tool.name"] = tool["tool_name"]
        _content_attr(ctx, attrs, "input.value", tool.get("input_json", ""))
        out.append(Span(
            trace_id=trace_id, span_id=tool["span_id"],
            parent_span_id=tool["parent_span_id"], name=tool["tool_name"],
            kind_oi="TOOL", start_ns=tool["start_ns"], end_ns=now_ns,
            attributes=attrs, status_code="UNSET", status_message="",
            pending=False,
        ))
    scope["open_tools"] = {}
    scope["open_task_spans"] = []
    return out


def finalize_session(state: dict, ctx: Ctx, now_ns: int) -> List[Any]:
    """Close everything still open, so no span of the trace stays pending."""
    trace_id = trace_id_for(ctx.session_id)
    out = finalize_turn(state, ctx, now_ns)
    for scope in [state] + list((state.get("sub_scopes") or {}).values()):
        if scope.get("open_tools"):
            out += _close_open_tools(scope, ctx, trace_id, now_ns)
    state["finalized"] = True
    if not state.get("root_started"):
        # build() never saw an entry: the user opened a session in an enabled
        # folder, typed nothing and quit. There is no root span to close --
        # closing one anyway emits a span starting at root_start_ns == 0, the
        # Unix epoch, which draws as a 56-year bar.
        return out
    root_span_id = span_id_for("session:" + ctx.session_id)
    attrs = _base_attrs(ctx, "AGENT")
    out.append(Span(
        trace_id=trace_id, span_id=root_span_id, parent_span_id=None,
        name="claude-code session", kind_oi="AGENT",
        start_ns=state.get("root_start_ns") or now_ns,
        end_ns=now_ns, attributes=attrs, status_code="OK", status_message="",
        pending=False,
    ))
    return out
