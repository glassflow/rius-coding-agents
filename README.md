<p align="center">
  <img src="docs/assets/banner.png" alt="Rius by GlassFlow: Claude Code sessions, traced" width="100%">
</p>

# Rius for Claude Code

A Claude Code plugin that streams your sessions to [Rius](https://www.glassflow.ai/rius)
as OpenTelemetry GenAI traces. Each session becomes one trace: turns, model
generations with token counts, tool calls and subagents, laid out as a
waterfall that fills in while the session is still running.

<p>
  <a href="https://docs.glassflow.ai/rius"><b>Docs</b></a> ·
  <a href="https://console.rius-glassflow.com">Console</a> ·
  <a href="docs/getting-started.md">Getting started</a> ·
  <a href="CHANGELOG.md">Changelog</a> ·
  <a href="https://github.com/glassflow/rius-coding-agents/issues">Issues</a>
</p>

[![CI](https://img.shields.io/github/actions/workflow/status/glassflow/rius-coding-agents/ci.yml?branch=main&style=flat-square&label=CI)](https://github.com/glassflow/rius-coding-agents/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-6b7280?style=flat-square)](LICENSE)
![Claude Code plugin](https://img.shields.io/badge/Claude_Code-plugin-d97757?style=flat-square)
![OpenTelemetry](https://img.shields.io/badge/OpenTelemetry-OTLP-425cc7?style=flat-square&logo=opentelemetry&logoColor=white)
![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-3776ab?style=flat-square&logo=python&logoColor=white)
![Zero runtime deps](https://img.shields.io/badge/dependencies-zero-6b7280?style=flat-square)

<!-- SCREENSHOT SLOT: a Claude Code session's waterfall in the Rius console,
     captured on production with the workspace name masked. Save it as
     docs/assets/console-trace.png and replace this comment with:
     <img src="docs/assets/console-trace.png" alt="A Claude Code session as a trace waterfall in the Rius console" width="100%"> -->

## What Rius sends

Rius is built for Claude Code: the terminal, the desktop app and the IDE
extensions. It doesn't run in claude.ai or Cowork and sees nothing from
them.

The same repository also ships beta support for two other coding agents,
[Codex](#codex-beta) and [Cursor](#cursor-beta). They send the same traces to
the same Rius hosts described below, and each keeps its own sign-in, key and
settings. Everything in this README describes Claude Code unless it says
otherwise.

Installing it sends nothing. You sign in with `/rius:login`, then choose
each folder to trace. Only you can run these commands: Claude can't run them
for you, and a repository's settings can't turn tracing on.

- **Structure only** (`/rius:enable-here`, the default): the span tree,
  model names, token counts, timing, status, tool names, error types, the
  kind of command a shell call ran (`test`, `build`, ...), a subagent's
  type, the folder path and git branch, and the email you signed in with at
  `/rius:login`. No prompts, replies, tool inputs or outputs, subagent
  briefs or session name.
- **With content** (`/rius:enable-content-here`): all of the above, plus
  your prompts, Claude's replies (never its thinking), tool inputs and tool
  outputs, which include file contents and command output.

A folder enabled before 0.5.0 keeps sending content until you run one of the
two commands again; `/rius:status` says so. `RIUS_CAPTURE_CONTENT=false`
turns content off everywhere.

### Secrets in content mode

Before export, the plugin replaces the secrets it recognises with a marker
such as `[redacted:aws-key]`: AWS, GCP, GitHub, Slack, Stripe, OpenAI,
Anthropic and Rius keys, JWTs, private keys, `password=`, `secret=`,
`token=` and `api_key=` style values, and Authorization or Bearer headers.
The output of a read of `.env*`, `*.pem`, `*.key`, `id_rsa*`,
`credentials*`, `.npmrc`, `.pypirc` or `.netrc` is replaced whole. This is
best effort: a secret in a format it doesn't know is sent. Each value is
capped at 32 KB.

### Fields

| Field | Example | Sent |
|---|---|---|
| `service.name`, `service.instance.id` | `claude-code`, a random id per session | always |
| `cc.version`, `cc.cwd`, `cc.git_branch` | `2.1.289`, `/Users/ana/src/shop` (can include your user name), `main` | always |
| span name, ids, start and end time, status | trace and span ids are hashes of the session id | always |
| `session.id`, `cc.claude_session_id`, `cc.continued_from` | Claude Code session ids | always |
| `user.id` | the email you signed in with at `/rius:login`, so the console can list your sessions; none for a key stored with `use-key` | always |
| `openinference.span.kind`, `gen_ai.operation.name`, `gen_ai.provider.name` | `LLM`, `chat`, `anthropic` | always |
| `gen_ai.request.model`, `gen_ai.response.model`, `gen_ai.response.finish_reasons` | `claude-opus-5-5`, `["tool_use"]` | always |
| `gen_ai.usage.*` | input, output, reasoning, cache read and cache write token counts | always |
| `rius.context.sizes` | byte sizes of the prompt by role and tool, no text | always |
| `gen_ai.tool.name` | `Bash`, `mcp__github__create_issue` | always |
| `error.type`, `exception.type` | `Bash.exit_1`, `Read.tool_error` | always |
| `rius.command.class`, `process.exit.code` | `test`, `1`: the kind of command a shell call ran (`test`, `build`, `lint`, `package`, `git` or `other`), worked out on your machine, and its exit code when known. The command is not sent | always |
| `gen_ai.agent.name`, `cc.subagent.id`, `cc.subagent.depth`, `cc.turn.source` | `Explore`, `user` | always |
| `glassflow.span.pending` | `true` while a span is still running | always |
| `input.value`, `output.value` | prompt, reply, tool input and output | content mode |
| `gen_ai.agent.description` | a subagent's description | content mode |
| session span name | the session title instead of `claude-code session` | content mode |
| `exception.message`, failed tool status | one scrubbed line of the failing output; otherwise `tool error (detail withheld: content capture off)` | content mode |

The plugin sends token counts. Rius prices them at API rates, so the cost in
the console isn't what you pay on a Pro or Max subscription.

### Where it goes

| Host | When | What |
|---|---|---|
| `ingest.eu.console.rius-glassflow.com` | while a traced session runs | spans, and a heartbeat every 15 seconds for at most 12 hours: a random instance id, `claude-code`, the time, the plugin's SDK name and version. No content. |
| `connect.console.rius-glassflow.com` | `/rius:login`, `/rius:logout` | your computer's hostname (first 64 characters, shown on the approval page), a one-time device code while you approve, and the key once more to revoke it |
| `mcp.eu.console.rius-glassflow.com` | only when you use the Rius MCP tools | your questions, signed in separately through `/mcp` |

Nothing else is contacted. Everything goes over https, redirects are never
followed, and the key is only sent to the Rius host it was issued for. The
key is stored in `~/.claude/rius/credentials.json`, readable only by you,
and is never logged. The plugin is Python standard library only. It honours
your environment's proxy and certificate settings, so a repo you mark as
trusted can change how that traffic is routed. Only trust repos you'd let
run code.

Data is stored in the EU and kept as described in your plan; see
[pricing](https://www.glassflow.ai/pricing) and the
[privacy policy](https://www.glassflow.ai/privacy-policy).

### Pricing

Rius is a paid service with a free trial that needs no card. Plans and
limits are on the [pricing page](https://www.glassflow.ai/pricing). When the
trial ends or a workspace is paused, Rius refuses new data, and the next
session start and `/rius:status` tell you so.

### Turn it off and uninstall

1. `/rius:off` stops this session; `/rius:disable-here` stops a folder.
2. `/rius:logout` revokes the key and deletes it from this machine. Run it
   **before** uninstalling, or the key stays valid until it expires.
3. `/plugin uninstall rius`
4. `rm -rf ~/.claude/rius/` removes folder rules, logs and session state,
   which uninstalling leaves behind.

## What runs on your machine

- **Hooks.** Claude Code runs `scripts/hook.sh` on session start, each
  prompt, each tool call, stop and session end. When you're signed out and
  nothing is open, it exits without starting Python.
- **An exporter per hook event** in a traced session: a detached Python
  process that reads the transcript, sends the new spans and exits.
- **One heartbeat process per traced session.** It pings every 15 seconds
  and stops when the session ends, when Claude Code exits, or after 12
  hours.
- **A process lookup**, `ps` on macOS and Linux or the Windows process
  APIs, used only to find Claude Code's own process so the heartbeat knows
  when the session is gone.
- **A browser window at `/rius:login`**, on a desktop session only. Over SSH
  or with no display, you open the printed link yourself.

Nothing runs at install. The plugin never downloads code or installs
packages: it is Python standard library only. See
[Updating](#updating) for how new versions reach you.

## Quick start

Install from a terminal:

```
claude plugin marketplace add glassflow/rius-coding-agents && claude plugin install rius@rius-coding-agents
```

Or inside Claude Code, one command at a time (pasting both lines together
fails, because Claude Code reads the paste as a single command):

```
/plugin marketplace add glassflow/rius-coding-agents
```

```
/plugin install rius@rius-coding-agents
```

Then, in Claude Code (run `/reload-plugins` first if it was already open),
run each of these on its own:

```
/rius:login
```

```
/rius:enable-here
```

```
/rius:status
```

`/rius:login` opens the Rius console (`https://console.rius-glassflow.com`)
in the browser, where you sign in or sign up and pick the workspace this
machine sends to; the plugin stores a key for it. That key only sends
traces. To ask Claude about your traces, also run `/mcp`, pick `rius` and sign
in there (see [Asking Claude about your traces](#asking-claude-about-your-traces)).
Over SSH, or on a machine with no display, no browser is opened: open the
printed link on any device and check that it shows the same code.
If `/rius:status` doesn't say `on`, the
[getting started guide](docs/getting-started.md) covers the rest end to end:
workspace, key and scopes, endpoints, staging, first trace, the Rius MCP
server, and troubleshooting.

Runs on macOS, Linux and Windows (through Git Bash) with any Python 3.9+ on
`PATH`. The plugin never runs a Python interpreter from inside the project
folder, or from a relative `PATH` entry, because a repository can set `PATH`
for every hook. Shell tools such as `ps` come from your `PATH`, the same as
for Claude Code itself. To
choose the interpreter yourself, put its absolute path on the first line of
`~/.claude/rius/python` (for example `/opt/homebrew/bin/python3`, or
`C:/Python312/python.exe` on Windows). [Installing and updating](docs/install.md) covers the upgrade from the
old `rius-claude-code` name, working from a local checkout, and platform
details.

## Updating

Claude Code does not update plugins from this marketplace by itself:
auto-update is off by default for marketplaces outside Anthropic's own. To
get a new version, refresh the marketplace and update the plugin from a
terminal:

```
claude plugin marketplace update rius-coding-agents && claude plugin update rius@rius-coding-agents
```

Or inside Claude Code, run `/plugin`, open the **Marketplaces** tab, pick
`rius-coding-agents` and choose **Update marketplace**. Then restart Claude
Code, or run `/reload-plugins`. On that same screen you can choose **Enable
auto-update**, so Claude Code keeps the plugin updated from then on.

## What you see in Rius

- **One trace per session.** Session, then turn, then model generation, then
  tool call. Subagents nest under the tool call that started them, with
  their own generations and tools.
- **Tokens and cost.** Every generation carries its model and token counts,
  including cache reads, and Rius prices them.
- **Live sessions.** Spans appear as they start, and a 15-second heartbeat
  keeps a long turn from reading as dead. A session that crashes mid-run
  stays visibly unfinished, and your next session closes it once it has
  been silent for 12 hours.
- **Failed tool calls marked as errors**, typed by tool and exit code (for
  example `Bash.exit_1`) so the console groups them by cause, with the line
  that explains why (unless content capture is off).
- **Your traces from inside Claude Code**, through the bundled Rius MCP server.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/how-it-works-dark.svg">
  <img alt="Claude Code hooks feed the rius plugin, which sends OTLP to Rius. The console and the Rius MCP server read the traces back, and the MCP server answers questions inside Claude Code." src="docs/assets/how-it-works-light.svg" width="100%">
</picture>

[How it works](docs/how-it-works.md) has the span tree, how subagents are
followed, the heartbeat, and why generation durations run long.

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
[What gets sent](#what-rius-sends).

You also need a key, from `/rius:login`. Without one, tracing stays off
regardless of any other setting.

## `/rius:*` commands

| Command | Effect |
|---|---|
| `/rius:login` | Sign in in the browser, pick the workspace this machine sends to, and store a key for it. Production by default; `/rius:login --env staging` for staging. |
| `/rius:enable-here` | Add the current working directory to the persistent path rules, sending structure only. This is the normal way to turn tracing on, and only you can run it. |
| `/rius:enable-content-here` | The same, but the folder also sends prompts, file contents and command output, with secrets removed. Only you can run it. |
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

Any other action prints its usage and exits 0. Every command passes
`--cwd` for you, so `enable-here`, `disable-here` and `status` always see the
folder you are actually in.

Only you can run these commands: Claude cannot invoke them on its own, so
text in a repository cannot talk it into turning tracing on. Each action
also accepts only its own flags and values (`/rius:login` takes just
`--env production` or `--env staging`) and refuses anything else without
changing anything.

`/rius:status` is the important one. Because the default is off, a plugin
that's installed correctly but simply not enabled for this folder looks
identical, from the outside, to a plugin that's broken. `status` disambiguates
by printing not just on/off but **which layer decided it** -- for example:

```
Rius tracing: off
Reason: off: no path rule matches /Users/you/some/repo, and the default is off
Next: run /rius:enable-here (or /rius:enable-content-here to include content)
```

versus

```
Rius tracing: off
Reason: off: no API key: run `/rius:login`
```

Those are different problems (folder not enabled vs. missing key), and the
`Reason` line is what tells you which one you have.

You also see one line from Rius when a session starts, so you know whether
it is on without asking:

- `Rius: tracing this session to workspace acme (content: on)` in a folder
  that is traced.
- `Rius: run /rius:login to start tracing` in a folder you enabled before
  signing in.
- `Rius: your workspace wasn't accepting data last time (trial ended or
  paused). If you've fixed it, this clears on the next upload.` once the
  backend has refused your data (HTTP 402). It stays until an export
  succeeds again, and `/rius:status` shows it too.
- `Rius installed: run /rius:login, then /rius:enable-here`, once ever, after
  you install. If you never sign in or enable a folder, Rius says nothing
  after that.

A session sees each line once. Resuming, clearing or compacting it shows the
line again only when what it says has changed. It is a status line for you,
sent as the hook's `systemMessage`, and carries no instructions for Claude.

`status` also prints the platform implementation that is live, the endpoint,
`Spans exported this session`, and -- whenever an export has failed -- a
`Last export error` line carrying the reason and the time. The
[troubleshooting section](docs/getting-started.md#troubleshooting) of the
getting started guide maps those reasons onto fixes, along with the
`~/.claude/rius/log/` files to read when `status` itself is not enough.

## Settings

What is traced, with which key and where to, is decided only by things you
do yourself: `/rius:login`, `/rius:enable-here`, `/rius:disable-here`,
`/rius:on` and `/rius:off`, all stored under `~/.claude/rius/`.

The settings below are environment variables, read from the environment
Claude Code passes to hook processes. That environment includes the `env`
block of a project's `.claude/settings.json`, which is committed with the
repo, so any repo you clone can set them. That is why the environment can
only turn things **off**: it cannot turn tracing on, turn content capture
back on, or choose the key or the server the key is sent to.

`RIUS_API_KEY` and `RIUS_ENDPOINT` are ignored. The key comes only from
`/rius:login`, and it is only ever sent to the server stored with it, which
must be `https` on one of the hosts Rius runs for that environment. When a setting is ignored, `/rius:status` says
so in one line, and the session's start is logged in `~/.claude/rius/log/`.

| Variable | Default | Meaning |
|---|---|---|
| `RIUS_SERVICE_NAME` | `claude-code` | Sets the `service.name` resource attribute. Letters, digits, `.`, `_` and `-` only, up to 64; anything else is ignored. |
| `RIUS_CLAUDE_ENABLED` | unset | `false` turns tracing off, over every path rule; only `/rius:on` beats it for one session. `true` is ignored: run `/rius:enable-here` to trace a folder. |
| `RIUS_CAPTURE_CONTENT` | unset | Can only lower capture. `false` sends structure only from every folder, whatever it chose (a failed tool's status reads `tool error (detail withheld: content capture off)`). `true` is ignored: run `/rius:enable-content-here` in a folder to send its content. |
| `RIUS_CLAUDE_MAX_ATTR_BYTES` | `32768` | Per-value truncation cap for content attributes, so a large file read doesn't break the export. It can only be lowered. Truncated values carry an explicit `…[truncated N bytes]` marker. |
| `RIUS_CLAUDE_DEBUG` | `false` | Verbose logging to `~/.claude/rius/log/`, including the detached exporter's and heartbeat pinger's own stderr (`spawn.log`). |

Unhandled exceptions are written to `~/.claude/rius/log/` **regardless of
`RIUS_CLAUDE_DEBUG`**. This is deliberate. Every hook exits 0 and the exporter
swallows its exceptions by design, so a crash that left no trace would be
invisible to everyone, forever; a log line is the only thing that isn't.
Normal operation writes nothing there unless debug is on.

Resolution order (first decision wins): a per-session `/rius:on`/`/rius:off`
override, then `RIUS_CLAUDE_ENABLED=false` from the environment, then the
path rules in `~/.claude/rius/config.json` (a disabled folder beats any
enabled parent), then the global default of off. Without a `/rius:login`
key, tracing is forced off no matter what the above resolves to.

## Your own API key

`/rius:login` is all most people need. To mint a key in the console instead,
or to decide where a hand-minted key should live, see
[API keys](docs/api-keys.md).

## Local Rius stack

For a Rius stack running on this machine (GlassFlow engineers run one from
the internal compose setup), make a key in its console and store it with
`pbpaste | bash <plugin>/scripts/rius_ctl.sh use-key --env local`. Traces then
go to `http://localhost:4318`; `/rius:login` has no local sign-in. The bundled
MCP entry stays production; add the local one with
`claude mcp add --transport http rius-local http://localhost:8082/mcp`.

## Asking Claude about your traces

The plugin bundles the Rius MCP server as `rius`, so you can query the
traces it produces from inside Claude Code. It connects to
`https://mcp.eu.console.rius-glassflow.com/mcp` and signs in with Claude
Code's own MCP OAuth, separately from `/rius:login`:

| Sign-in | For | How |
|---|---|---|
| `/rius:login` | Tracing: hooks send sessions to the workspace you pick | A key stored in `~/.claude/rius/credentials.json` |
| `/mcp`, then `rius` | Querying traces: Claude reads them back | Your Rius account through OAuth, kept by Claude Code |

`/rius:status` tells you how to sign in to each, one line apiece. The OAuth sign-in reaches every
workspace your account can, so name the workspace in your question when you
have more than one.

With it connected, questions like "which of my sessions
in the last 24 hours cost the most, and what did the tokens go on?" or "show
the waterfall for my last errored trace and say which tool call failed" are
answerable in chat. The
[getting started guide](docs/getting-started.md#exploring-your-traces-from-claude-code)
lists the main tools and how to register the server without the plugin.

## Codex (beta)

The same plugin traces [Codex](https://developers.openai.com/codex) CLI
sessions (tested with codex-cli 0.144.1). Each session becomes one trace:
turns, model calls with token counts (cached and reasoning tokens split
out), tool calls with failed commands marked as errors, and subagents under
the call that spawned them. A resumed session continues its trace.

```
codex plugin marketplace add glassflow/rius-coding-agents
codex plugin add rius@rius-coding-agents
```

Then, in Codex:

1. Open `/hooks` and trust the five `rius` hooks. Codex runs no plugin hook
   until you do, so nothing is traced before this step.
2. Type `$rius:rius-login`, then `$rius:rius-enable-here` in a folder you
   want traced. Codex never runs these skills on its own. Both need to write `~/.codex/rius`, so approve
   the sandbox escalation Codex asks for.
3. `$rius:rius-status` shows what is on, why, and whether the hooks are
   trusted.
4. To query your traces from Codex, run `codex mcp login rius` once. The
   bundled MCP server signs in with OAuth only, as in Claude Code.

Tracing is off by default and the content rules above apply unchanged.
Codex keeps its own login, key and settings in `~/.codex/rius`, separate
from Claude Code's, and always under your home folder, whatever
`CODEX_HOME` says. See [docs/codex.md](docs/codex.md) for details,
the MCP sign-in and the known gaps.

## Cursor (beta)

The repo also ships a Cursor plugin (`.cursor-plugin/`). It traces Cursor
agent sessions: one trace per conversation, with a span for each turn, model
answer, tool call and subagent. Install it with "Import from Repo"
(`glassflow/rius-coding-agents`) in Cursor's plugin settings, then run
`/rius-login` and `/rius-enable-here` in the agent chat. To query your
traces, connect the bundled `rius` MCP server in Cursor's MCP settings; it
signs in with OAuth only.

- **No token counts or cost.** Cursor's hooks do not report usage.
- **Beta.** It is tested against payloads from real `cursor-agent`
  sessions; the IDE agent and plugin import are not verified yet.
- Its key, settings and state are its own, under `~/.cursor/rius/`.

[docs/cursor.md](docs/cursor.md) has the details, including a fallback for
older `cursor-agent` builds that ignore plugin hooks.

## Beyond Claude Code

Rius traces other agents as well. The
[Python and TypeScript SDKs](https://docs.glassflow.ai/rius/sdk/installation)
have integrations for the
[Claude Agent SDK](https://docs.glassflow.ai/rius/sdk/integrations/claude-agent-sdk),
the [OpenAI Agents SDK](https://docs.glassflow.ai/rius/sdk/integrations/openai-agents),
[LangChain](https://docs.glassflow.ai/rius/sdk/integrations/langchain),
[CrewAI](https://docs.glassflow.ai/rius/sdk/integrations/crewai),
[Pydantic AI](https://docs.glassflow.ai/rius/sdk/integrations/pydantic-ai)
and more. Anything that already emits OpenTelemetry, whether through
[OpenLLMetry](https://docs.glassflow.ai/rius/interoperability/openllmetry),
[OpenInference](https://docs.glassflow.ai/rius/interoperability/openinference),
a plain [OTel SDK](https://docs.glassflow.ai/rius/interoperability/otel-sdks)
or a [Collector](https://docs.glassflow.ai/rius/interoperability/collector),
can send to Rius over OTLP.

## Documentation

| Page | What it covers |
|---|---|
| [Getting started](docs/getting-started.md) | Workspace, key and scopes, endpoints, staging, first trace, MCP, troubleshooting |
| [Installing and updating](docs/install.md) | Marketplace cache, upgrading from `rius-claude-code`, local checkout, platforms |
| [How it works](docs/how-it-works.md) | The span tree, subagents, live spans, generation timing, heartbeat |
| [API keys](docs/api-keys.md) | Minting a key by hand and where to keep it |
| [Cursor (beta)](docs/cursor.md) | The Cursor plugin: install, commands, what is and is not traced |
| [Design records](docs/design/) | The original spec, plan and build log |
| [Changelog](CHANGELOG.md) | What changed in each version |
| [Rius docs](https://docs.glassflow.ai/rius) | The product: console, alerts, MCP tools, SDKs |

## Contributing

Bug reports and pull requests are welcome. The plugin is standard-library
Python and the suite runs with:

```
python -m pip install 'pytest>=7.0' 'opentelemetry-proto==1.43.0'
python -m pytest -q
claude plugin validate .
```

`opentelemetry-proto` is test-only: it checks that the OTLP/JSON the plugin
sends decodes into the real OTLP messages, and never ships.

## License

[MIT](LICENSE). Built by [GlassFlow](https://www.glassflow.ai).
