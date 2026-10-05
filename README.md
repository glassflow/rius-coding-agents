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

## What gets sent -- read this before enabling anything

Nothing is sent for any folder until you enable it (see
[Enabling a folder](#enabling-a-folder)). Installing the plugin uploads
nothing. When you enable a folder, you choose one of two modes for it:

- **Structure only** (`/rius:enable-here`, the default and the
  recommendation). The span tree, model names, token counts (including cache
  reads), cost, timing, status, tool names and error types, a subagent's
  type and model, and the folder path and git branch. No prompts, no
  assistant text, no tool inputs or outputs, no subagent briefs or
  descriptions, no session name (the trace is titled `claude-code session`)
  and no line of a failed tool's output.
- **With content** (`/rius:enable-content-here`). All of the above,
  plus every prompt, assistant message, tool input and tool **output**: the
  contents of files Claude Code reads and the output of commands it runs.
  Before export, the plugin replaces the secrets it recognises with a
  marker such as `[redacted:aws-key]`: AWS, GCP, GitHub, Slack, Stripe,
  OpenAI, Anthropic and Rius keys, JWTs, private keys, `password=`,
  `secret=`, `token=` and `api_key=` style values, and Authorization or
  Bearer headers. The output of a read of `.env*`, `*.pem`, `*.key`,
  `id_rsa*`, `credentials*`, `.npmrc`, `.pypirc` or `.netrc` is dropped
  whole. Recognising is not a guarantee: a secret in a format it does not
  know is sent.

Run `/rius:enable-here` or `/rius:enable-content-here` again to change a
folder's mode. A folder enabled before this choice existed keeps sending
content until you do, and `/rius:status` says so. `RIUS_CAPTURE_CONTENT=false`
turns content off for every folder.

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
If `/rius:status` doesn't say `on`, the
[getting started guide](docs/getting-started.md) covers the rest end to end:
workspace, key and scopes, endpoints, staging, first trace, the Rius MCP
server, and troubleshooting.

To update later, refresh the marketplace, because Claude Code caches it.
Run these one at a time:

```
/plugin marketplace update rius-coding-agents
```

```
/reload-plugins
```

Runs on macOS, Linux and Windows (through Git Bash) with any Python 3.9+ on
`PATH`. The plugin never runs a Python from inside the project folder, or from
a relative `PATH` entry, because a repository can set `PATH` for every hook. To
choose the interpreter yourself, put its absolute path on the first line of
`~/.claude/rius/python` (for example `/opt/homebrew/bin/python3`, or
`C:/Python312/python.exe` on Windows). [Installing and updating](docs/install.md) covers the upgrade from the
old `rius-claude-code` name, working from a local checkout, and platform
details.

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
[What gets sent](#what-gets-sent----read-this-before-enabling-anything).

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

Any other action prints its usage line and exits 0. Every command passes
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
```

versus

```
Rius tracing: off
Reason: off: no API key: run `/rius:login`
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
| `RIUS_ENV` | `production` | The environment `/rius:login` signs in to: `production` or `staging`. `--env` wins over it. |
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

## Privacy and security posture

- Zero runtime dependencies: the plugin is Python standard library only,
  nothing is pulled from PyPI at install or run time.
- Span exports go only to the endpoint stored with the `/rius:login` key,
  and only when it is an `https` Rius host. `/rius:login` refuses to store a
  key for any other server. `/rius:login`, `/rius:logout` and a
  re-login's revoke of the previous key also call the sign-in host
  (`connect.console.rius-glassflow.com`, or
  `connect.staging.rius.glassflow.xyz` on staging), and the bundled MCP
  server talks to `mcp.eu.console.rius-glassflow.com` when you use it.
  Nothing else is contacted.
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

## Documentation

| Page | What it covers |
|---|---|
| [Getting started](docs/getting-started.md) | Workspace, key and scopes, endpoints, staging, first trace, MCP, troubleshooting |
| [Installing and updating](docs/install.md) | Marketplace cache, upgrading from `rius-claude-code`, local checkout, platforms |
| [How it works](docs/how-it-works.md) | The span tree, subagents, live spans, generation timing, heartbeat |
| [API keys](docs/api-keys.md) | Minting a key by hand and where to keep it |
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

`opentelemetry-proto` is test-only: it checks the hand-written OTLP encoder
against the real wire format and never ships.

## License

[MIT](LICENSE). Built by [GlassFlow](https://www.glassflow.ai).
