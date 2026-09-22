# rius-claude-code

A Claude Code plugin that streams Claude Code sessions to [Rius](https://glassflow.dev)
as OTLP GenAI traces: one trace per session, with turns, model generations
(including token counts), tool calls, and subagents laid out as a waterfall.

## What gets sent -- read this before enabling anything

By default this plugin captures **full session content**: every user prompt,
every assistant message, every tool input, and every tool **output**. Tool
output means the actual contents of files Claude Code read and the actual
output of commands it ran. If a session happens to `cat` a `.env` file or read
a config with a credential in it, that content is captured and sent to your
Rius workspace like any other tool output.

Two things bound this:

1. **Tracing is off by default.** Nothing is sent for any folder until you
   explicitly enable it (see [Enabling a folder](#enabling-a-folder) below).
   Installing the plugin does not, by itself, upload anything.
2. **`RIUS_CAPTURE_CONTENT=false`** turns off content capture entirely, for
   every folder you've enabled. You still get full structure: span
   hierarchy, model names, token counts (including cache reads), cost, timing
   and status. You lose prompt text, assistant text, and tool input/output
   values.

Only enable a folder you're comfortable having its file reads and command
output leave the machine, or set `RIUS_CAPTURE_CONTENT=false` first.

## Install

### Local development

```
/plugin marketplace add /opt/glass0/claude-observe
/plugin install rius-claude-code@rius-coding-agents
```

The marketplace name is `rius-coding-agents` and the plugin name is
`rius-claude-code`, as declared in `.claude-plugin/marketplace.json`. There is
no file watcher: after editing anything under `hooks/` or `scripts/`, run
`/reload-plugins` for the change to take effect in your current Claude Code
process.

### From GitHub

```
/plugin marketplace add glassflow/rius-coding-agents
/plugin install rius-claude-code@rius-coding-agents
```

This works against a private `github.com/glassflow/rius-coding-agents` repo
using your existing git credentials -- no extra auth step is needed if you can
already `git clone` the repo.

## Enabling a folder

Tracing defaults to off everywhere. To turn it on for the folder you're
currently in:

```
/rius enable-here
```

This adds your current working directory to the path rules stored in
`~/.claude/rius/config.json`. Every subdirectory under it is enabled too. The
default-off behavior exists so that installing the plugin can never silently
start uploading a repo nobody has thought about -- see
[What gets sent](#what-gets-sent----read-this-before-enabling-anything).

You also need `RIUS_API_KEY` set (see [Settings](#settings)). Without it,
tracing stays off regardless of any other setting.

## `/rius` command

| Command | Effect |
|---|---|
| `/rius on` | Turn tracing on for the current session only, overriding path rules and env vars. |
| `/rius off` | Turn tracing off for the current session only, same override strength as `on`. |
| `/rius clear` | Remove the per-session override, falling back to the normal resolution (env vars, then path rules, then off). |
| `/rius enable-here` | Add the current working directory to the persistent path rules. |
| `/rius status` | Print the resolved on/off state, the endpoint, the redacted API key, and spans exported so far this session. |

`/rius status` is the important one. Because the default is off, a plugin
that's installed correctly but simply not enabled for this folder looks
identical, from the outside, to a plugin that's broken. `status` disambiguates
by printing not just on/off but **which layer decided it** -- for example:

```
Rius tracing: off
Reason: off: no path rule matches /Users/you/some/repo, and the default is off
```

versus

```
Rius tracing: off
Reason: off: RIUS_API_KEY is not set
```

Those are different problems (folder not enabled vs. missing key), and the
`Reason` line is what tells you which one you have.

## Settings

All settings are environment variables, read from the environment Claude Code
passes to hook processes (which includes values from `~/.claude/settings.json`
and project `.claude/settings.json`/`settings.local.json`).

| Variable | Default | Meaning |
|---|---|---|
| `RIUS_API_KEY` | unset | Required. Without it, tracing is disabled regardless of every other setting. |
| `RIUS_ENDPOINT` | `https://ingest.eu.console.rius-glassflow.com` | Base URL only, no path. The plugin appends `/v1/traces` and `/v1/heartbeat` itself. |
| `RIUS_SERVICE_NAME` | `claude-code` | Sets the `service.name` resource attribute. |
| `RIUS_CLAUDE_ENABLED` | unset | Per-folder on/off override, normally set via `.claude/settings.json` or `settings.local.json` rather than by hand. |
| `RIUS_CAPTURE_CONTENT` | `true` | `false` drops prompt/message/tool-input/tool-output content; structure, models, tokens, cost, and timing are kept either way. |
| `RIUS_CLAUDE_MAX_ATTR_BYTES` | `32768` | Per-value truncation cap for content attributes, so a large file read doesn't break the export. Truncated values carry an explicit `…[truncated N bytes]` marker. |
| `RIUS_CLAUDE_DEBUG` | `false` | Verbose logging to `~/.claude/rius/log/`. |

Resolution order (first decision wins): a per-session `/rius on`/`/rius off`
override, then `RIUS_CLAUDE_ENABLED` from the environment, then the path rules
in `~/.claude/rius/config.json`, then the global default of off. If
`RIUS_API_KEY` is unset, tracing is forced off no matter what the above
resolves to.

## What a trace looks like

One trace per Claude Code session, shaped as a waterfall:

```
AGENT   session root
└─ CHAIN  turn (one user prompt and everything it caused)
   └─ LLM   generation (one assistant message: model, token counts, cache reads)
      └─ TOOL  tool call (input and output)
         └─ AGENT  subagent, nested under the Task tool span that spawned it
```

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
the preceding entry, and its duration ends up absorbing whatever happened
before the call actually went out: user think time, a permission prompt,
queuing behind a tool call. If you compare a generation's duration against
what you'd expect from your Anthropic bill or API logs, expect it to run
long, sometimes by a lot. There is no signal in the transcript that would let
this be tightened without guessing.

## Heartbeat

A small background process pings the endpoint every 15 seconds while a
session is active, so a long-running agent turn doesn't read as dead in the
UI just because no span has closed recently. It starts when the session
starts and exits on its own once the session ends or Claude Code exits --
nothing is left running in the background afterward.

## Privacy and security posture

- Zero runtime dependencies: the plugin is Python standard library only,
  nothing is pulled from PyPI at install or run time.
- Network traffic goes only to the configured `RIUS_ENDPOINT`, nothing else.
- The API key is never logged. `/rius status` and debug logs print it
  redacted (prefix plus an ellipsis).

## Uninstall

```
/plugin uninstall rius-claude-code
```

Local state -- session overrides, path rules, per-session span-count state,
and debug logs -- lives entirely under `~/.claude/rius/` and is not removed
by uninstalling the plugin. Delete that directory by hand if you want a clean
slate:

```
rm -rf ~/.claude/rius/
```
