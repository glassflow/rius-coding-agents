# Rius for Cursor (beta)

The same repo ships a Cursor plugin next to the Claude Code one. It traces
Cursor agent sessions (the IDE agent and `cursor-agent`) to Rius.

**Beta.** The plugin is built and tested against Cursor's documented hook
payloads and the payload code in `cursor-agent` 2026.02.13. It has **not**
yet been run against a live Cursor session, so field names may still change
once real payloads are captured.

## What you get, and what you don't

You get one trace per Cursor conversation:

```
cursor session                AGENT  sessionStart .. sessionEnd
  turn                        CHAIN  one per prompt, prompt .. stop
    <model>                   LLM    the answer; provider worked out from the model name
    <tool>                    TOOL   preToolUse .. postToolUse / postToolUseFailure
      <subagent type>         AGENT  a subagent, under the Task call that started it
```

- Failed tools carry `error.type`. A shell command that exits non-zero gives
  `Shell.exit_<code>`. Any other failure gives `<tool>.<failure_type>`, for
  example `MCP:query.timeout`. An interrupted tool is not an error.
- Spans carry `service.name=cursor`, plus `cursor.version`, `cursor.cwd` and
  the composer mode.

**No token counts and no cost.** Cursor's hooks do not report token usage,
so Cursor LLM spans have no `gen_ai.usage.*` attributes and Rius shows no
cost for them. The one size figure Cursor gives, the context size before a
compaction, is sent as `cursor.context.*`.

Other gaps:

- Times are when each hook fired, not when the model call started.
- Cloud and background agents fire no session hooks.
- Tab completions are not traced.

## What gets sent

The rules are the same as for Claude Code. See
[What gets sent](../README.md#what-gets-sent----read-this-before-enabling-anything).
Tracing is off until you enable a folder, and `RIUS_CAPTURE_CONTENT=false`
keeps prompts, answers, and tool input and output out of the spans.

Each hook appends the event to a local spool at
`~/.cursor/rius/spool/<conversation>.jsonl` (mode 0600). With capture off,
no content is ever written to the spool. Your email and transcript paths are
never written there.

## Install

Pick one:

- **From the repo (team marketplace).** In Cursor's plugin settings, use
  "Import from Repo" with `glassflow/rius-coding-agents`. Cursor reads
  `.cursor-plugin/marketplace.json`.
- **Local folder.** Clone the repo into `~/.cursor/plugins/local/rius`.

The Cursor manifest names its own hooks, commands and MCP file under
`cursor/`, so Cursor never loads the Claude Code files.

**Older `cursor-agent` builds.** CLI releases before 2026-08-27 ignore hooks
that come from a plugin. For those, write the same hooks into your own
`~/.cursor/hooks.json`:

```
python3 <plugin>/scripts/rius_ctl.py install-hooks --agent cursor
```

This merges into the file and leaves your other hooks alone. Running it
again changes nothing. Running it after an upgrade replaces the old paths.
`uninstall-hooks --agent cursor` takes the Rius hooks back out. `--path <file>`
targets another hooks file, such as a project's `.cursor/hooks.json`.

## Sign in and enable a folder

In the agent chat:

1. `/rius-login` signs you in in the browser and stores a key for Cursor in
   `~/.cursor/rius/credentials.json`. This key is separate from the Claude
   Code key, and `/rius-logout` revokes only the Cursor key.
2. `/rius-enable-here` turns on tracing for the current folder.

`RIUS_API_KEY` in the environment wins over the stored key, as it does in
Claude Code.

| Command | What it does |
|---|---|
| `/rius-login`, `/rius-logout` | Sign in, or sign out and revoke the key |
| `/rius-status` | Whether this folder is traced, and why |
| `/rius-enable-here`, `/rius-disable-here` | Trace this folder and everything under it, or stop |
| `/rius-on`, `/rius-off` | This chat only |

The commands are prompts: the agent runs `scripts/rius_ctl.sh` through its
Shell tool, so Cursor may ask you to approve the command. The plugin's
`sessionStart` hook tells the session where the plugin lives
(`RIUS_PLUGIN_ROOT`) and which conversation it is (`RIUS_CURSOR_SESSION_ID`).
Cursor's CLI hands those variables to later hooks but not always to the
Shell tool, so the hook also puts both values in the chat's context, on a
line starting `Rius plugin:`, and the commands use that when the variables
are empty. If a command still cannot find the script, start a new chat or
run the same command from a terminal.

Settings use the `RIUS_CURSOR_` prefix where Claude Code uses
`RIUS_CLAUDE_`: `RIUS_CURSOR_ENABLED`, `RIUS_CURSOR_DEBUG`,
`RIUS_CURSOR_MAX_ATTR_BYTES`. Logs go to `~/.cursor/rius/log/`.

## Querying traces from Cursor (MCP)

`cursor/mcp.json` registers the Rius MCP server
(`https://mcp.eu.console.rius-glassflow.com/mcp`) with no key, so Cursor signs
in with OAuth: use Connect in the MCP settings, or
`cursor-agent mcp login rius`. This sign-in is separate from `/rius-login`.

To use an API key instead, add your own entry to `~/.cursor/mcp.json`:

```json
{"mcpServers": {"rius": {"url": "https://mcp.eu.console.rius-glassflow.com/mcp",
  "headers": {"Authorization": "Bearer ${env:RIUS_API_KEY}"}}}}
```

## If you also have the Claude Code plugin

Cursor runs Claude Code plugin hooks by default. The Claude Code hook
recognises a Cursor payload and does nothing, so a Cursor session is never
traced twice or sent as an empty "claude-code session".

## When spans are sent

The exporter runs in the background on `sessionStart`, on each `stop`, on
`subagentStop`, on `sessionEnd`, and after every 20 finished tools. Each run
sends only the spans that changed since the last export. A conversation that
never sent `sessionEnd` is closed by the next session's start once it has
been idle for 12 hours.

## Uninstall

Remove the plugin in Cursor's plugin settings, and run `uninstall-hooks` if
you used `install-hooks`. Local state lives under `~/.cursor/rius/`:

```
rm -rf ~/.cursor/rius/
```
