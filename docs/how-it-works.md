# How it works

What the plugin turns a Claude Code session into, and the limits of what the
transcript lets it know.

## The trace

One trace per Claude Code session, shaped as a waterfall:

```
AGENT   session root
└─ CHAIN  turn (one user prompt and everything it caused)
   └─ LLM   generation (one model API response: model, token counts, cache reads)
      ├─ TOOL  tool call (input and output)
      └─ TOOL  Agent (the call that spawned a subagent)
         └─ AGENT  the subagent, named by its agent type
            └─ LLM   the subagent's own generation
               └─ TOOL  a tool the subagent called
```

**One generation per API response.** Claude Code writes a single model
response as several transcript lines, one per content block (thinking, text,
each tool call), and every line repeats the response's token usage. The
plugin groups the lines by their `message.id`, so each response is one `LLM`
span with its usage counted once, and every tool it called hangs beneath it.
A response whose lines arrive over several hook events is re-sent under the
same span id; the backend keeps the latest copy.

**Each generation says what its prompt held**, as sizes, in
`rius.context.sizes` (the attribute the Rius SDKs send). The console's
Context panel uses it to split a call's prompt into user and assistant
history, the current turn, and each tool's calls and results. Only byte
sizes and tool names are sent, so it is sent with content capture off too.
It is an estimate from the transcript: Claude Code's system prompt, its tool
definitions and the reminders it injects are not in the transcript, and the
panel shows them as unattributed.

**Subagents are drilled into.** Claude Code does not write a subagent's work
into the session transcript -- each subagent gets its own file under
`~/.claude/projects/<project>/<session-id>/subagents/`, and the sibling
`.meta.json` names the exact tool call that spawned it. The plugin follows
that link and emits the subagent as an `AGENT` span under the tool span, with
its generations and tool calls beneath. This is not a detail: in the session
this was built against, **58% of all tokens and 71% of all model calls were
inside subagents**, and a trace that stopped at the tool call reported less
than half of what the session cost.

Background subagents are followed to the end. Claude Code 2.1.x runs a
subagent in the background by default: its `Agent` tool call returns a launch
acknowledgement within a second, and the subagent keeps working for minutes
afterwards. The plugin keeps reading the subagent's file as it grows, so its
model calls land under its `AGENT` span, and that span runs from the
subagent's first transcript line to its last rather than ending at the
acknowledgement.

The subagent's span carries `gen_ai.agent.name` (its agent type, e.g.
`general-purpose`), its own model and its description, so each subagent is
filterable as a named agent in the Rius UI rather than an anonymous span.
Subagents that spawn subagents nest the same way, to a bounded depth.

Turns the harness injected rather than you typing them -- a slash command's
caveat, a background task's notification, a subagent's report -- keep their
span but are marked `cc.turn.source = system`, so the turn list can tell them
apart from your prompts.

Spans appear while the session is still running, not only after it ends: each
hook event emits a "pending" snapshot at span start (session start, prompt
submit, tool start) that the backend shows as in-progress, then replaces with
the finished span once the corresponding end event arrives. A session that
dies mid-run leaves those pending spans unresolved -- that's intentional, it's
what "the agent died while running" is supposed to look like in the UI, not a
bug to be papered over.

## Generation timing is approximate

Tool span durations are accurate: start and end come from real transcript
timestamps (the entry carrying the `tool_use` and the matching
`tool_result`).

Generation span durations are not. The Claude Code transcript only records
*completion* times -- nothing in it marks when a request was actually
dispatched to the model. So a generation's start is taken as the timestamp of
the entry before its first line, its end is the timestamp of its last line, and its duration ends up absorbing whatever happened
before the call actually went out: user think time, a permission prompt,
queuing behind a tool call. If you compare a generation's duration against
what you'd expect from your Anthropic bill or API logs, expect it to run
long, sometimes by a lot. There is no signal in the transcript that would let
this be tightened without guessing.

## A conversation moved to a new session id

When Claude Code sends a session to the background, it continues the
conversation under a new session id. It records the move in the old
transcript (a `continued-in` entry naming the new id) and starts the new
transcript with a copy of the whole history. The plugin follows that record:
the new session's trace starts with the first new entry, skips the copied
history the old session already sent, and carries `cc.continued_from` with the
old session id. The old session gets no `SessionEnd`, so its trace is
closed at the moment of the move. The conversation is then two traces, one
before the move and one after, with nothing counted twice.

## Heartbeat

A small background process pings the endpoint every 15 seconds while a
session is active, so a long-running agent turn doesn't read as dead in the
UI just because no span has closed recently. It starts when the session
starts and exits on its own once the session ends or Claude Code exits --
nothing is left running in the background afterward.

Each ping carries an `instance_id` that covers exactly one Claude Code
process lifetime -- never two. A **resumed** session is a new process, so it
gets a fresh `instance_id` even though it continues the same session (and
therefore the same trace): the old instance already sent its
`stopped: true` ping when the prior process exited, so reusing its id would
have this new process contradict it by pinging as "already stopped."
**Compacting** or **clearing** context, by contrast, happens inside the
same running process, so the existing `instance_id` is kept. In short: a
resumed session continues the same trace but reports as a new instance.
