# Getting started

Zero to a first trace: get a Rius workspace and an API key, install the
plugin, sign in, enable one folder, and confirm spans are
arriving. Every step below is something you do once.

Read [What gets sent](../README.md#what-gets-sent----read-this-before-enabling-anything)
in the README before you enable any folder. By default this plugin sends
full session content, including the contents of files Claude Code reads and
the output of commands it runs.

- [Quick start: `/rius:login`](#quick-start-riuslogin)
- [1. Get a Rius workspace](#1-get-a-rius-workspace)
- [2. Mint an API key](#2-mint-an-api-key)
- [3. Pick an endpoint](#3-pick-an-endpoint)
- [4. Install the plugin](#4-install-the-plugin)
- [5. Sign in](#5-sign-in)
- [6. Enable the folder](#6-enable-the-folder)
- [7. Watch the first trace](#7-watch-the-first-trace)
- [Exploring your traces from Claude Code](#exploring-your-traces-from-claude-code)
- [Troubleshooting](#troubleshooting)
- [Uninstall and local state](#uninstall-and-local-state)

## Quick start: `/rius:login`

Steps 1, 2, 3 and 5 below collapse into one command. Install from a
terminal:

```
claude plugin marketplace add glassflow/rius-coding-agents && claude plugin install rius@rius-coding-agents
```

(Inside Claude Code, run `/plugin marketplace add glassflow/rius-coding-agents`
and `/plugin install rius@rius-coding-agents` as two separate commands --
pasting both together fails.) Then, in Claude Code, one at a time:

```
/rius:login
```

Sign in in the browser and pick a workspace. Then, in the folder you want
traced:

```
/rius:enable-here
```

`/rius:login` prints a short code and opens the Rius portal
(`https://console.rius-glassflow.com`). There you sign
in (or sign up), check that the page shows the same code and this machine's
name, and pick the workspace this machine should send to: one of yours, or
one you are only a member of. The plugin then prints where it landed:

```
Connected as x@acme.com → eng-shared (Acme).
Tracing: signed in as workspace eng-shared (Acme)
Querying traces: run /mcp and sign in to rius
Trace this folder (/path)? Run /rius:enable-here.
```

The key is stored in `~/.claude/rius/credentials.json` (mode 0600) with the
region's ingest endpoint, and expires after 90 days. It never goes into
Claude Code's settings. Signing in again replaces the key and revokes the old
one.

- This is the only key the plugin traces with. `RIUS_API_KEY` and
  `RIUS_ENDPOINT` in the environment are ignored, and `/rius:status` says so
  when either is set. See [5. Sign in](#5-sign-in) for why.
- `/rius:logout` revokes the key on the server, then deletes it locally. If
  the server cannot be reached it still signs out, and says the key stays
  valid until it expires.
- Tracing is still off until `/rius:enable-here`. Login never enables a
  folder. `/rius:disable-here` turns a folder (and everything under it) off
  again, even inside an enabled parent.
- The key only sends traces. Querying them from Claude Code is a second,
  separate sign-in: run `/mcp`, pick `rius` and sign in with your Rius
  account (OAuth). See
  [Exploring your traces](#exploring-your-traces-from-claude-code).
- A key minted seconds ago can be rejected with 401 for up to ~30s while it
  propagates to the receiver.

### Signing in to staging

Production is the default. Staging is only reached when you ask for it:

```
/rius:login --env staging
```

or with `RIUS_ENV=staging` in the environment Claude Code runs in (`--env`
wins over `RIUS_ENV`). An unknown name is refused rather than sent to
production. The key stored afterwards remembers its environment, so its
ingest endpoint, `/rius:logout` and the revoke on the next sign-in all go
to staging too.

The bundled MCP server is the exception: it always connects to production.
To query staging traces, register the staging server yourself under its own
name and sign in to it in `/mcp`:

```bash
claude mcp add --transport http rius-staging https://mcp.eu.staging.rius.glassflow.xyz/mcp
```

### Upgrading from `rius-claude-code`

Versions before 0.3.0 installed the plugin as `rius-claude-code`. Uninstall
it first, or both copies stay installed: every hook fires twice (duplicate
spans) and every command appears under both names. From a terminal:

```
claude plugin uninstall rius-claude-code@rius-coding-agents && claude plugin marketplace update rius-coding-agents && claude plugin install rius@rius-coding-agents
```

Path rules and the stored key live under `~/.claude/rius/` and carry over.

## 1. Get a Rius workspace

Rius has two containers that are easy to confuse:

- An **organization** holds membership, roles and billing.
- A **workspace** is what traces and API keys actually belong to. A key is
  scoped to exactly one workspace, never to a whole organization.

You get your first workspace by logging in to the Rius console in a browser.
Sign-in is an Auth0 browser flow; there is no self-serve signup API and no
way to bootstrap an account from the command line. The first time an
identity with no workspaces lists its workspaces, a workspace named
`Default` is created for it, so for most people "log in to the console" is
the whole step.

Creating any *further* workspace explicitly is restricted to organization
admins and requires a region to be chosen for it.

> **The Rius MCP server cannot create a workspace.** It has no tool for it.
> Workspace creation lives in the console (or the control-plane API), so a
> headless agent cannot provision itself one.

## 2. Mint an API key

The plugin itself only uses a key stored in `~/.claude/rius/` (see
[5. Sign in](#5-sign-in)). To use one you mint here, store it with
`rius_ctl.sh use-key` ([API keys](api-keys.md#where-the-api-key-goes)).

In the console: workspace settings, then API keys, then create a key.

A key looks like `ri_<id>.<signature>`. Older keys issued with a `gf_`
prefix are still accepted, so an existing `gf_` key does not need to be
replaced; new keys are issued as `ri_`.

The plaintext key is shown exactly once, at creation. It is stored hashed,
so it cannot be recovered afterwards. If you lose it, mint another.

**Scopes.** One key carries a set of scopes, and the two you care about are:

| Scope | Needed by | What it is for |
|---|---|---|
| `ingest` | this plugin | Sending spans to the ingest endpoint. |
| `read` | the Rius MCP server, when you give it a key | Reading traces, metrics and incidents back. |

This plugin needs only `ingest`; its bundled MCP server signs in with OAuth
and needs no key. A key may hold both, for example to register the MCP
server headless with a bearer header. An ingest-only key sent to the MCP
server is rejected with a 403 -- see [Troubleshooting](#troubleshooting).

**Expiry** is chosen at creation from a fixed menu: never, 30 days, 90 days
or 1 year. It defaults to never. There is no extend operation; to change it,
mint a replacement and revoke the old key.

> **The first key must come from a browser login.** An API key can never
> mint another API key -- that restriction is enforced in the backend so
> that a leaked agent key cannot enumerate or create credentials. So the
> first key has to come from a human session: either the console, or one
> interactive OAuth session against the Rius MCP server, whose
> `create_api_key` tool can mint keys for you afterwards. A key-authenticated
> MCP session gets a 403 from that tool.

There is no rotate or revoke tool on the MCP server either. Rotation is
"mint a replacement, switch over, revoke the old one in the console".

## 3. Pick an endpoint

You do not pick it by hand: `/rius:login` stores the ingest endpoint of the
environment you sign in to, and the plugin sends the key nowhere else. It
must be an `https` host under that environment's Rius domain (or this
machine, for development); `/rius:login` refuses any other. The plugin
appends `/v1/traces` and `/v1/heartbeat` itself.

| Environment | Ingest endpoint | MCP server | Console |
|---|---|---|---|
| Production (default) | `https://ingest.eu.console.rius-glassflow.com` | `https://mcp.eu.console.rius-glassflow.com/mcp` | `https://console.rius-glassflow.com` |
| Staging | `https://ingest.eu.staging.rius.glassflow.xyz` | `https://mcp.eu.staging.rius.glassflow.xyz/mcp` | `https://staging.rius.glassflow.xyz` |

The production hosts are the plugin's built-in defaults. A key from
`/rius:login` carries its own ingest endpoint, so staging needs no setting
either. For the staging MCP server, see
[Signing in to staging](#signing-in-to-staging).

Staging and production are separate deployments with separate stores, so
expect a key to work only against the deployment that issued it. Mint a
separate key per environment rather than trying to reuse one.

## 4. Install the plugin

For everyday use, install from git. From a terminal:

```
claude plugin marketplace add glassflow/rius-coding-agents && claude plugin install rius@rius-coding-agents
```

Or inside Claude Code, as two separate commands. Run one at a time: pasting
both together fails, because Claude Code reads the paste as one command.

1. ```
   /plugin marketplace add glassflow/rius-coding-agents
   ```
2. ```
   /plugin install rius@rius-coding-agents
   ```
3. ```
   /reload-plugins
   ```

For development against a local checkout, point the marketplace at the
directory instead:

```
claude plugin marketplace add /path/to/rius-coding-agents && claude plugin install rius@rius-coding-agents
```

The marketplace is named `rius-coding-agents` and the plugin
`rius`, matching `.claude-plugin/marketplace.json` and
`.claude-plugin/plugin.json`.

> **Refresh the marketplace after every plugin change.** Claude Code caches
> a marketplace added from a directory or a git source rather than reading
> it live, so after the plugin is updated -- upstream or in your own
> checkout -- you are still running whatever was cached when you installed
> until you run these, one at a time:
>
> ```
> /plugin marketplace update rius-coding-agents
> ```
>
> ```
> /reload-plugins
> ```
>
> This is not a formality. A stale cache looks exactly like a change that
> did not work: the code on disk is new, the code being run is old, and
> nothing anywhere says so. It invalidated a full round of testing during
> development of this plugin.

`/reload-plugins` on its own is enough for edits to `hooks/` or `scripts/`
in a local directory install that the marketplace has already re-read. There
is no file watcher; nothing reloads by itself.

## 5. Sign in

The plugin takes its key only from `/rius:login`:

```
/rius:login
```

It stores the key in `~/.claude/rius/credentials.json` (mode 0600), with
the ingest endpoint of the environment you signed in to, and never sends
that key anywhere else. A key in the environment is not used: Claude Code
hands hooks the `env` block of a project's committed `.claude/settings.json`,
so a repo you clone could otherwise send your sessions to its own
workspace, or your key to its own server. `RIUS_API_KEY` and
`RIUS_ENDPOINT` are ignored, and `/rius:status` says so when either is set.

To trace with a key you minted in the console instead, store it with
`rius_ctl.sh use-key` (see [API keys](api-keys.md#where-the-api-key-goes)).
No key reaches the bundled MCP server, which signs in with OAuth. A
hand-minted key is what you use to register the MCP server yourself (see
[Exploring your traces](#exploring-your-traces-from-claude-code)) or for the
Rius SDKs.

The full list of variables is in the README's
[Settings](../README.md#settings) table.

## 6. Enable the folder

Tracing is off for every folder until you say otherwise:

```
/rius:enable-here
```

That appends the current working directory to `enabled_paths` in
`~/.claude/rius/config.json`, and every subdirectory under it is covered
too.

## 7. Watch the first trace

```
/rius:status
```

A working setup prints `Rius tracing: on`, a `Reason` line naming the path
rule that decided it, the endpoint, the redacted key, and a span count that
grows as the session goes on:

```
Rius tracing: on
Reason: on: path rule '/Users/you/some/repo' enables /Users/you/some/repo
cwd: /Users/you/some/repo
session: 0f1d...
Platform: darwin (locking: fcntl.flock, liveness: os.kill(pid, 0))
Endpoint: https://ingest.eu.console.rius-glassflow.com
Tracing: signed in as workspace eng-shared (Acme)
Querying traces: run /mcp and sign in to rius
API key: ri_…
Spans exported this session: 12
```

Send a prompt or two, then open the trace in the Rius console. Spans appear
while the session is still running: each hook event emits a pending snapshot
at span start, replaced by the finished span when the end event arrives.

If `Spans exported this session` stays at 0, go to
[Troubleshooting](#troubleshooting).

## Exploring your traces from Claude Code

The Rius MCP server lets you ask Claude questions about the traces this
plugin is producing, from inside Claude Code. The plugin bundles it as
`rius`, at `https://mcp.eu.console.rius-glassflow.com/mcp`, so there is
nothing to install: run `/mcp`, pick `rius` and sign in with your Rius
account. That is Claude Code's own MCP OAuth, separate from `/rius:login`,
and Claude Code keeps the token. It reaches every workspace your account
can, so name the workspace in your question when you have more than one.

To use the MCP server without the plugin, or with a key you minted by hand,
register it yourself -- **as an HTTP URL server,
not a stdio command server**. That is the single most common setup mistake,
and it fails in a way that reads as an auth problem. The examples name it
`glassflow`, so it does not share a name with the plugin's bundled `rius`;
with both installed, both entries show in `/mcp`.

Interactive (OAuth in the browser, same account as the console):

```bash
claude mcp add --transport http glassflow https://mcp.eu.console.rius-glassflow.com/mcp
```

Headless, with a `read`-scoped key as a static bearer header:

```bash
claude mcp add --transport http glassflow https://mcp.eu.console.rius-glassflow.com/mcp \
  --header "Authorization: Bearer ${RIUS_API_KEY}"
```

Use the staging host from [3. Pick an endpoint](#3-pick-an-endpoint) for a
staging key. Use `/mcp` inside Claude Code to trigger or inspect the OAuth
flow.

`/mcp` lists every tool the server exposes. These are the ones worth
knowing once traces are flowing:

| Tool | What it answers |
|---|---|
| `get_me` | Which identity and workspace this credential is. |
| `list_workspaces` | Workspaces this credential can reach. |
| `list_agent_traces` | Recent traces, filtered by window, service or status. |
| `get_agent_trace` | The span waterfall for one trace, with its errors. |
| `agent_traces_summary` | Counts, error rate, cost, tokens and latency for a window. |
| `workspace_metrics_overview` | The same, fuller, with deltas and breakdowns. |
| `agent_error_groups` | A window's failures grouped by error signature. |
| `list_alerts` | Alerts Rius raised for this workspace. |
| `create_api_key` | Mint another workspace key. Interactive sessions only. |

Questions this makes answerable in chat:

- "Which of my Claude Code sessions in the last 24 hours cost the most, and
  what did the tokens go on?"
- "Show me the waterfall for my last errored trace and tell me which tool
  call failed."
- "Group this week's failures by error signature and show the last run of
  that step that succeeded."

There is no rotate or revoke tool, and no workspace-create tool.

## Troubleshooting

Start with `/rius:status`. Because the default is off, a correctly installed
but not-yet-enabled plugin looks identical from the outside to a broken one,
and the `Reason` line is what separates the two. It names the layer that
decided, verbatim:

| `Reason` line | What it means |
|---|---|
| `off: no path rule matches <cwd>, and the default is off` | The folder was never enabled. Run `/rius:enable-here`. |
| ``off: no API key: run `/rius:login` `` | The folder is enabled, but no `/rius:login` key is stored. |
| ``<reason>; also no API key: run `/rius:login` `` | Tracing is off for `<reason>`, and it would stay off without a key even once that is fixed. |
| ``off: no API key: the stored key's server <url> is not a Rius server, so it is never sent there; run `/rius:login` `` | `credentials.json` names a server that is not an `https` Rius host, so its key is not used. Sign in again. |
| `off: RIUS_CLAUDE_ENABLED` | `RIUS_CLAUDE_ENABLED=false` in the environment is turning it off, above the path rules. |
| `off: session override` | A `/rius:off` override is in force for this session. `/rius:on` flips it; a new session starts without it. |
| `on: path rule '<rule>' enables <cwd>` | Working as intended, unless a `Stopped:` line follows (see below). |

A `Stopped:` line under `Reason` means this session's folder was disabled
while it was running. That session sends nothing more, even if the folder is
enabled again, and `Rius tracing` reads `off` for it whatever the rules say;
its trace is still closed when it ends. Start a new session to trace again.

Then read the rest of the output:

- **`Spans exported this session`** is the count that matters. 0 with
  tracing on means nothing has reached the endpoint yet.
- **`Last export error`** appears whenever an export failed, with the
  reason and a timestamp. It distinguishes a rejected key (HTTP 401/403), no
  receiver at the stored endpoint (404), an unreachable host, and a rate
  limit.
- **`... is set but ignored`** lines name each environment setting that
  asked for something only you can grant, such as `RIUS_API_KEY` or
  `RIUS_CLAUDE_ENABLED=true`. They change nothing; they say why a setting
  has no effect.
- **`Platform`** names the code path that is live, which is the quickest way
  to tell whether a Windows machine is running the Windows implementation.

Common causes, in the order they actually happen:

| Symptom | Cause | Fix |
|---|---|---|
| A plugin change has no effect | The marketplace cache is stale | `/plugin marketplace update rius-coding-agents`, then `/reload-plugins` |
| `Last export error: rejected the API key (HTTP 403)` right after editing a key's scopes | Granting `ingest` takes about 30 seconds to reach the receiver | Wait half a minute and send another prompt |
| The MCP server returns 403 but the key works for ingest | The key has `ingest` but not `read` | Mint a key carrying `read`, or add the scope |
| `/mcp` shows `rius` as needing authentication | The bundled server signs in with OAuth, separately from `/rius:login` | Pick `rius` in `/mcp` and sign in with your Rius account |
| The MCP server shows no auth support, or will not connect | It was registered as a stdio command server, not an HTTP URL | Re-add it with `--transport http` and a URL |
| A local MCP dev server fails the TLS handshake | `https://` against a plain-HTTP local port | Use `http://` for local ports |
| Auth fails on a self-hosted deployment with no obvious reason | The Auth0 audience or the email-claim key does not match the configured string exactly | Both are exact-string matches; compare them character for character with the deployment's configuration |
| Nothing at all happens, and `/rius:status` prints nothing useful | No Python interpreter was found | Check `~/.claude/rius/log/bootstrap.log` |

Logs live in `~/.claude/rius/log/`:

- `<date>.log` -- normal debug output, written only when
  `RIUS_CLAUDE_DEBUG=true`, plus unhandled exceptions, which are written
  regardless of the debug setting. Every hook exits 0 by design, so a crash
  that logged nothing would be invisible.
- `spawn.log` -- the detached exporter's and heartbeat's own stderr, with
  debug on.
- `bootstrap.log` -- written by the shell launcher when it cannot find a
  Python interpreter or cannot locate its own directory. It names the
  interpreters it tried. If this file exists, nothing else in the plugin
  ever ran.

## Uninstall and local state

```
/plugin uninstall rius
```

Local state -- path rules, per-session overrides, span counts and logs --
lives under `~/.claude/rius/` and survives uninstalling. Remove it by hand
for a clean slate:

```bash
rm -rf ~/.claude/rius/
```

Revoking the API key in the console is a separate step, and the only one
that stops the credential working.
