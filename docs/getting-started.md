# Getting started

Zero to a first trace: get a Rius workspace and an API key, install the
plugin, point it at an endpoint, enable one folder, and confirm spans are
arriving. Every step below is something you do once.

Read [What gets sent](../README.md#what-gets-sent----read-this-before-enabling-anything)
in the README before you enable any folder. By default this plugin sends
full session content, including the contents of files Claude Code reads and
the output of commands it runs.

- [Quick start: `/rius:login` (staging)](#quick-start-rius-login-staging)
- [1. Get a Rius workspace](#1-get-a-rius-workspace)
- [2. Mint an API key](#2-mint-an-api-key)
- [3. Pick an endpoint](#3-pick-an-endpoint)
- [4. Install the plugin](#4-install-the-plugin)
- [5. Put the key where Claude Code will find it](#5-put-the-key-where-claude-code-will-find-it)
- [6. Enable the folder](#6-enable-the-folder)
- [7. Watch the first trace](#7-watch-the-first-trace)
- [Exploring your traces from Claude Code](#exploring-your-traces-from-claude-code)
- [Troubleshooting](#troubleshooting)
- [Uninstall and local state](#uninstall-and-local-state)

## Quick start: `/rius:login` (staging)

On staging, steps 1, 2, 3 and 5 below collapse into one command:

```
/plugin marketplace add glassflow/rius-coding-agents
/plugin install rius@rius-coding-agents
/rius:login          # approve the code in the browser; sign up if you are new
/rius:enable-here    # in the folder you want traced
```

`/rius:login` uses Auth0's device flow: it prints a short code, opens the
browser, and once you approve it exchanges your sign-in for an API key with
`ingest` + `read` scopes in your workspace (a brand-new account gets a
`Default` workspace created on the spot). The key is stored in
`~/.claude/rius/credentials.json` (mode 0600) together with the staging
ingest endpoint, and it expires after a year. It never goes into Claude
Code's settings.

- `RIUS_API_KEY` in the environment still wins over the stored key, so an
  existing setup keeps working unchanged. `/rius:status` says which one is
  in use (`Key from:`).
- `/rius:logout` deletes the stored key locally. It stays valid on the
  server until it expires or is revoked in the console.
- Tracing is still off until `/rius:enable-here`. Login never enables a
  folder.
- The plugin bundles the Rius MCP server as `rius`, authenticated with the
  same key (via a `headersHelper`, so the key never lands in any MCP
  config). After logging in, reconnect it in `/mcp` or restart Claude Code.
  `RIUS_MCP_URL` points it elsewhere; it defaults to staging.
- A key minted seconds ago can be rejected with 401 for up to ~30s while it
  propagates to the receiver.
- Production has no device-flow application yet; there, follow the steps
  below.

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
| `read` | the Rius MCP server | Reading traces, metrics and incidents back. |

A key may hold both. If you plan to install the MCP server as well as this
plugin, mint one key with `ingest` and `read` and use it for both. An
ingest-only key sent to the MCP server is rejected with a 403 -- see
[Troubleshooting](#troubleshooting).

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

`RIUS_ENDPOINT` is a base URL with no path. The plugin appends `/v1/traces`
and `/v1/heartbeat` itself.

| Environment | Ingest endpoint (`RIUS_ENDPOINT`) | Console |
|---|---|---|
| Production (default) | `https://ingest.eu.console.rius-glassflow.com` | confirm with your Rius admin |
| Staging | `https://ingest.staging.rius.glassflow.xyz` | `https://staging.rius.glassflow.xyz` |

The production ingest host is the plugin's built-in default, so on
production you do not need to set `RIUS_ENDPOINT` at all. The matching
production console URL is deliberately not printed here: it could not be
confirmed from a single authoritative source, and a wrong URL in a setup
guide is worse than an absent one. Ask a Rius admin, or start on staging,
where both halves are known.

Staging and production are separate deployments with separate stores, so
expect a key to work only against the deployment that issued it. Mint a
separate key per environment rather than trying to reuse one.

## 4. Install the plugin

For everyday use, install from git:

```
/plugin marketplace add glassflow/rius-coding-agents
/plugin install rius@rius-coding-agents
```

For development against a local checkout, point the marketplace at the
directory instead:

```
/plugin marketplace add /path/to/rius-coding-agents
/plugin install rius@rius-coding-agents
```

The marketplace is named `rius-coding-agents` and the plugin
`rius`, matching `.claude-plugin/marketplace.json` and
`.claude-plugin/plugin.json`.

> **Refresh the marketplace after every plugin change.** Claude Code caches
> a marketplace added from a directory or a git source rather than reading
> it live, so after the plugin is updated -- upstream or in your own
> checkout -- you are still running whatever was cached when you installed
> until you run:
>
> ```
> /plugin marketplace update rius-coding-agents
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

## 5. Put the key where Claude Code will find it

Everything is configured with environment variables, read from the
environment Claude Code hands to hook processes. That includes `env` values
from `~/.claude/settings.json` and from a project's
`.claude/settings.json` or `.claude/settings.local.json`.

Prefer the project-scoped `.claude/settings.local.json`:

```json
{
  "env": {
    "RIUS_API_KEY": "ri_xxxxxxxxxxxxxxxx.xxxxxxxxxxxxxxxx"
  }
}
```

Three reasons to keep the credential there rather than in the global
`~/.claude/settings.json`:

- `settings.local.json` is the per-project file that is conventionally
  gitignored, so the key does not follow the repo into a commit. Check that
  your repo does ignore it before writing a key into it.
- A key is scoped to one workspace. Projects that report into different
  workspaces need different keys, which a single global value cannot
  express.
- A key in the global file applies to every folder on the machine. Tracing
  is off by default per folder, so this is not an immediate leak, but it
  makes the blast radius of a later `/rius:enable-here` larger than it needs
  to be.

If you would rather not have the key in a settings file at all, export
`RIUS_API_KEY` in the shell that launches Claude Code. The plugin only reads
the environment; it has no credential file of its own.

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
plugin is producing, from inside Claude Code. It is a separate thing to
install; this plugin does not need it and does not depend on it.

**Register it as an HTTP URL server, not a stdio command server.** That is
the single most common setup mistake, and it fails in a way that reads as an
auth problem.

Interactive (OAuth in the browser, same account as the console):

```bash
claude mcp add --transport http rius https://mcp.staging.rius.glassflow.xyz/mcp
```

Headless, with a `read`-scoped key as a static bearer header:

```bash
claude mcp add --transport http rius https://mcp.staging.rius.glassflow.xyz/mcp \
  --header "Authorization: Bearer ${RIUS_API_KEY}"
```

Substitute the MCP host for your environment; the staging host is shown
because it is the one this guide can state with confidence. Use `/mcp`
inside Claude Code to trigger or inspect the OAuth flow.

The server exposes 13 tools. These are the ones worth knowing once traces
are flowing:

| Tool | What it answers |
|---|---|
| `get_me` | Which identity and workspace this credential is. |
| `list_workspaces` | Workspaces this credential can reach. |
| `list_agent_traces` | Recent traces, filtered by window, service or status. |
| `get_agent_trace` | The span waterfall for one trace, with its errors. |
| `agent_traces_summary` | Counts, error rate, cost, tokens and latency for a window. |
| `workspace_metrics_overview` | The same, fuller, with deltas and breakdowns. |
| `list_metrics_catalog` | Every metric the platform computes, with units. |
| `get_error_pattern` | A window's failures grouped by error signature. |
| `get_last_known_good` | The last successful run of a step before a cutoff. |
| `get_open_incidents` | Incidents Rius opened for repeating failures. |
| `update_incident_status` | Advance an incident's status, forward-only. |
| `log_resolution` | Append a resolution note to an incident. |
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
| `off: RIUS_API_KEY is not set` | The folder is enabled, but no key reached the hook process. |
| `off: RIUS_CLAUDE_ENABLED` | An environment variable is turning it off, above the path rules. |
| `off: session override` | A `/rius:off` override is in force for this session. `/rius:on` flips it; a new session starts without it. |
| `on: path rule '<rule>' enables <cwd>` | Working as intended. |

Then read the rest of the output:

- **`Spans exported this session`** is the count that matters. 0 with
  tracing on means nothing has reached the endpoint yet.
- **`Last export error`** appears whenever an export failed, with the
  reason and a timestamp. It distinguishes a rejected key (HTTP 401/403), a
  wrong base URL (404, "RIUS_ENDPOINT must be a BASE url"), an unreachable
  host, and a rate limit.
- **`Platform`** names the code path that is live, which is the quickest way
  to tell whether a Windows machine is running the Windows implementation.

Common causes, in the order they actually happen:

| Symptom | Cause | Fix |
|---|---|---|
| A plugin change has no effect | The marketplace cache is stale | `/plugin marketplace update rius-coding-agents`, then `/reload-plugins` |
| `Last export error: rejected the API key (HTTP 403)` right after editing a key's scopes | Granting `ingest` takes about 30 seconds to reach the receiver | Wait half a minute and send another prompt |
| The MCP server returns 403 but the key works for ingest | The key has `ingest` but not `read` | Mint a key carrying `read`, or add the scope |
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
