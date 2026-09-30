# rius

[![CI](https://github.com/glassflow/rius-coding-agents/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/glassflow/rius-coding-agents/actions/workflows/ci.yml)
![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-3776ab?style=flat-square&logo=python&logoColor=white)
![Zero runtime deps](https://img.shields.io/badge/dependencies-zero-6b7280?style=flat-square)

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

## Quick start

```
/plugin marketplace add glassflow/rius-coding-agents
/plugin install rius@rius-coding-agents
/rius:login
/rius:enable-here
/rius:status
```

`/rius:login` opens the Rius console (`https://console.rius-glassflow.com`)
in the browser, where you sign in or sign up and pick the workspace this
machine sends to; the plugin stores a key for it. That is the whole flow.
If `/rius:status` doesn't say `on`, the
[getting started guide](docs/getting-started.md) covers the rest end to end:
workspace, key and scopes, endpoints, staging, first trace, the Rius MCP
server, and troubleshooting.

## Install

### Local development

```
/plugin marketplace add /path/to/rius-coding-agents
/plugin install rius@rius-coding-agents
```

The marketplace name is `rius-coding-agents` and the plugin name is
`rius`, as declared in `.claude-plugin/marketplace.json`. There is
no file watcher: after editing anything under `hooks/` or `scripts/`, run
`/reload-plugins` for the change to take effect in your current Claude Code
process.

### Upgrading from `rius-claude-code`

Versions before 0.3.0 installed the plugin as `rius-claude-code`. Remove it
before installing `rius`, or both stay installed: every hook fires twice
(duplicate spans) and every command appears under both names.

```
/plugin uninstall rius-claude-code@rius-coding-agents
/plugin marketplace update rius-coding-agents
/plugin install rius@rius-coding-agents
```

Your path rules and stored key live under `~/.claude/rius/` and carry over.

### From GitHub

```
/plugin marketplace add glassflow/rius-coding-agents
/plugin install rius@rius-coding-agents
```

The repo is public, so no GitHub credentials are needed.

### Updating an installed plugin

> **Claude Code caches the marketplace. Refresh it after every plugin
> change.** A marketplace added from a directory or a git source is read
> once and cached, not read live, so an updated plugin -- upstream or in
> your own checkout -- does not reach your session until you run:
>
> ```
> /plugin marketplace update rius-coding-agents
> /reload-plugins
> ```
>
> Skipping this is not a cosmetic problem. A stale cache is indistinguishable
> from a change that did not work: the code on disk is new, the code being
> run is old, and nothing says so. It invalidated a full round of testing
> during development of this plugin, which is why it has a section of its
> own rather than a footnote.

### Platform support

macOS, Linux and Windows. The plugin is pure standard library, so the only
requirement is a Python 3.9+ on `PATH`.

On Windows the hook is launched through Git Bash, which Claude Code already
needs for its own Bash tool, and `scripts/hook.sh` picks the interpreter --
`py`, then `python`, then `python3`. If it cannot find one it writes a line
saying so to `~/.claude/rius/log/bootstrap.log` rather than doing nothing
quietly. `/rius:status` prints which platform implementation is live.

If Claude Code on your machine falls back to PowerShell because Git Bash is
not installed, the hooks will not run. That is the one configuration this
plugin does not yet cover.

CI runs the suite on Linux only, across Python 3.10 to 3.13 plus a 3.9
runtime-floor job. There is no Windows CI job yet (tracked as RIUS-945), so
the Windows code paths are covered by unit tests with fakes rather than by a
native run.

## Getting a Rius workspace and API key

The plugin cannot do anything without a Rius API key. `/rius:login` gets
you one without leaving Claude Code. To mint one by hand instead, for
`RIUS_API_KEY`, the short version is:

- Log in to the Rius console (`https://console.rius-glassflow.com`) in a
  browser. A `Default` workspace is created
  for an identity that has none the first time it lists workspaces, so for
  most people that is the whole workspace step. Creating further workspaces
  explicitly is restricted to organization admins.
- Mint a key in workspace settings. A key looks like `ri_<id>.<signature>`;
  older `gf_` keys still work. The plaintext is shown once and stored
  hashed. Expiry is chosen at creation from never, 30 days, 90 days or 1
  year, defaulting to never.
- Keys are scoped to one workspace and carry scopes. This plugin needs
  `ingest`. The Rius MCP server needs `read`. One key can hold both.

> **The first key has to come from a browser login.** An API key can never
> mint another API key -- the backend refuses, so that a leaked agent key
> cannot create more credentials. Either the console or a single interactive
> OAuth session against the Rius MCP server gets you the first one; after
> that the MCP server's `create_api_key` can mint further keys, but it
> cannot create a workspace and there is no revoke tool.

The [getting started guide](docs/getting-started.md) has the full version,
including which endpoint to use for which environment.

## Enabling a folder

Tracing defaults to off everywhere. To turn it on for the folder you're
currently in:

```
/rius:enable-here
```

This adds your current working directory to the path rules stored in
`~/.claude/rius/config.json`. Every subdirectory under it is enabled too. The
default-off behavior exists so that installing the plugin can never silently
start uploading a repo nobody has thought about -- see
[What gets sent](#what-gets-sent----read-this-before-enabling-anything).

You also need a key: `/rius:login`, or `RIUS_API_KEY` (see
[Settings](#settings)). Without one, tracing stays off regardless of any
other setting.

## `/rius:*` commands

| Command | Effect |
|---|---|
| `/rius:login` | Sign in in the browser, pick the workspace this machine sends to, and store a key for it. Production by default; `/rius:login --env staging` for staging. |
| `/rius:enable-here` | Add the current working directory to the persistent path rules. This is the normal way to turn tracing on. |
| `/rius:disable-here` | Stop tracing the current working directory and everything under it. A disabled folder beats any enabled parent. |
| `/rius:status` | Print the resolved on/off state, the rule that decided it, the signed-in account, the endpoint, the redacted API key, spans exported so far, and the last export error if there was one. |
| `/rius:logout` | Revoke the stored key on the server, then delete it locally. |
| `/rius:on` | Turn tracing on for this session only, overriding path rules and env vars. |
| `/rius:off` | Turn tracing off for this session only, same override strength as `on`. |

`/rius:on` and `/rius:off` pass the current session id themselves. Run from a
shell, `rius_ctl.sh on|off|clear` refuse to run without an explicit
`--session <id>`: inferring it from the most recently active session would
either do nothing useful on a fresh install or flip tracing for a different
session you happen to have open. Most of the time you want
`/rius:enable-here` instead, which is persistent and needs no session id.

Any other action prints its usage line and exits 0. Every command passes
`--cwd` for you, so `enable-here`, `disable-here` and `status` always see the
folder you are actually in.

`/rius:status` is the important one. Because the default is off, a plugin
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

`status` also prints the platform implementation that is live, the endpoint,
`Spans exported this session`, and -- whenever an export has failed -- a
`Last export error` line carrying the reason and the time. The
[troubleshooting section](docs/getting-started.md#troubleshooting) of the
getting started guide maps those reasons onto fixes, along with the
`~/.claude/rius/log/` files to read when `status` itself is not enough.

## Settings

All settings are environment variables, read from the environment Claude Code
passes to hook processes (which includes values from `~/.claude/settings.json`
and project `.claude/settings.json`/`settings.local.json`).

| Variable | Default | Meaning |
|---|---|---|
| `RIUS_API_KEY` | unset | Wins over the key `/rius:login` stored, for tracing. With neither, tracing is disabled regardless of every other setting. The bundled MCP server never sees it: Claude Code withholds credential-named variables from a plugin's `headersHelper`. |
| `RIUS_ENDPOINT` | `https://ingest.eu.console.rius-glassflow.com` | Base URL only, no path. The plugin appends `/v1/traces` and `/v1/heartbeat` itself. A key from `/rius:login` brings its own. |
| `RIUS_ENV` | `production` | The environment `/rius:login` signs in to: `production` or `staging`. `--env` wins over it. |
| `RIUS_MCP_URL` | `https://mcp.eu.console.rius-glassflow.com/mcp` | The bundled MCP server's URL. Set it for a staging key; `/rius:status` says when it does not match the stored key. |
| `RIUS_SERVICE_NAME` | `claude-code` | Sets the `service.name` resource attribute. |
| `RIUS_CLAUDE_ENABLED` | unset | Per-folder on/off override, normally set via `.claude/settings.json` or `settings.local.json` rather than by hand. |
| `RIUS_CAPTURE_CONTENT` | `true` | `false` drops prompt/message/tool-input/tool-output content, including a subagent's brief and description and a failed tool's output (its status reads `tool error (detail withheld: RIUS_CAPTURE_CONTENT=false)`); structure, models, tokens, cost, and timing are kept either way. |
| `RIUS_CLAUDE_MAX_ATTR_BYTES` | `32768` | Per-value truncation cap for content attributes, so a large file read doesn't break the export. Truncated values carry an explicit `…[truncated N bytes]` marker. |
| `RIUS_CLAUDE_DEBUG` | `false` | Verbose logging to `~/.claude/rius/log/`, including the detached exporter's and heartbeat pinger's own stderr (`spawn.log`). |

Unhandled exceptions are written to `~/.claude/rius/log/` **regardless of
`RIUS_CLAUDE_DEBUG`**. This is deliberate. Every hook exits 0 and the exporter
swallows its exceptions by design, so a crash that left no trace would be
invisible to everyone, forever; a log line is the only thing that isn't.
Normal operation writes nothing there unless debug is on.

Resolution order (first decision wins): a per-session `/rius:on`/`/rius:off`
override, then `RIUS_CLAUDE_ENABLED` from the environment, then the path rules
in `~/.claude/rius/config.json`, then the global default of off. If
`RIUS_API_KEY` is unset, tracing is forced off no matter what the above
resolves to.

### Where the API key goes

Put it in the project's `.claude/settings.local.json`, not in the global
`~/.claude/settings.json`:

```json
{
  "env": {
    "RIUS_API_KEY": "ri_xxxxxxxxxxxxxxxx.xxxxxxxxxxxxxxxx"
  }
}
```

`settings.local.json` is the per-project file that is conventionally
gitignored, so the credential does not follow the repo into a commit --
check that your repo does ignore it before writing a key there. A key is
also scoped to a single Rius workspace, so projects reporting into different
workspaces need different keys, which one global value cannot express. And a
key in the global file applies to every folder on the machine; tracing is
off per folder by default, so that is not a leak by itself, but it makes the
blast radius of a later `/rius:enable-here` wider than it needs to be.

Exporting `RIUS_API_KEY` in the shell that launches Claude Code works just
as well. Without it, the plugin uses the key `/rius:login` stored in
`~/.claude/rius/credentials.json` (mode 0600); `RIUS_API_KEY` wins when both
are present.

## How it works

One trace per Claude Code session, shaped as a waterfall:

```
AGENT   session root
└─ CHAIN  turn (one user prompt and everything it caused)
   └─ LLM   generation (one assistant message: model, token counts, cache reads)
      ├─ TOOL  tool call (input and output)
      └─ TOOL  Agent (the call that spawned a subagent)
         └─ AGENT  the subagent, named by its agent type
            └─ LLM   the subagent's own generation
               └─ TOOL  a tool the subagent called
```

**Subagents are drilled into.** Claude Code does not write a subagent's work
into the session transcript -- each subagent gets its own file under
`~/.claude/projects/<project>/<session-id>/subagents/`, and the sibling
`.meta.json` names the exact tool call that spawned it. The plugin follows
that link and emits the subagent as an `AGENT` span under the tool span, with
its generations and tool calls beneath. This is not a detail: in the session
this was built against, **58% of all tokens and 71% of all model calls were
inside subagents**, and a trace that stopped at the tool call reported less
than half of what the session cost.

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

### Generation timing is approximate

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

## Asking Claude about your traces

The plugin bundles the Rius MCP server as `rius`, so you can query the
traces it produces from inside Claude Code. It uses the key `/rius:login`
stored (through a `headersHelper`, so the key never lands in an MCP config;
`RIUS_API_KEY` never reaches it, because Claude Code withholds
credential-named variables from a plugin's helper) and
connects to `https://mcp.eu.console.rius-glassflow.com/mcp`. After
`/rius:login`, reconnect `rius` in `/mcp` or restart Claude Code.

A staging key needs `RIUS_MCP_URL` pointed at the staging MCP host before
Claude Code starts: `.mcp.json` can only expand environment variables, so
the plugin cannot follow the stored key there by itself. `/rius:login` and
`/rius:status` print the exact setting when it is needed.

The server needs a `read`-scoped key; a hand-minted ingest-only key is
rejected with a 403. With it connected, questions like "which of my sessions
in the last 24 hours cost the most, and what did the tokens go on?" or "show
the waterfall for my last errored trace and say which tool call failed" are
answerable in chat. The
[getting started guide](docs/getting-started.md#exploring-your-traces-from-claude-code)
lists the main tools and how to register the server without the plugin.

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

## Privacy and security posture

- Zero runtime dependencies: the plugin is Python standard library only,
  nothing is pulled from PyPI at install or run time.
- Span exports go only to the configured `RIUS_ENDPOINT` (or the endpoint
  stored with the `/rius:login` key). `/rius:login`, `/rius:logout` and a
  re-login's revoke of the previous key also call the sign-in host
  (`connect.console.rius-glassflow.com`, or
  `connect.staging.rius.glassflow.xyz` on staging), and the bundled MCP
  server talks to `RIUS_MCP_URL` when you use it. Nothing else is
  contacted.
- The API key is never logged. `/rius:status` and debug logs print it
  redacted (prefix plus an ellipsis).

## Uninstall

```
/plugin uninstall rius
```

Local state -- session overrides, path rules, per-session span-count state,
and debug logs -- lives entirely under `~/.claude/rius/` and is not removed
by uninstalling the plugin. Delete that directory by hand if you want a clean
slate:

```
rm -rf ~/.claude/rius/
```
