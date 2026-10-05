# Rius for Cursor (beta)

The same repo ships a Cursor plugin next to the Claude Code one. It traces
Cursor agent sessions (the IDE agent and `cursor-agent`) to Rius.

**Beta.** The plugin is tested against hook payloads captured from real
`cursor-agent` 2026.10.01 sessions: headless (`-p`) runs with tools and a
subagent, and an interactive session that ended before any tool ran. Tools
in an interactive session, the IDE agent and plugin import have not been
run yet.

## What you get, and what you don't

You get one trace per Cursor conversation:

```
cursor session                AGENT  sessionStart .. sessionEnd
  turn                        CHAIN  one per prompt, prompt .. stop
    <model>                   LLM    the answer; provider worked out from the model name
    <tool>                    TOOL   preToolUse .. postToolUse / postToolUseFailure
      <subagent type>         AGENT  a subagent, under the Task call that started it
```

- Failed tools carry `error.type`, `<tool>.<failure_type>`: a shell command
  that exits non-zero gives `Shell.error` (Cursor reports it as a failure
  without the exit code), a timed-out MCP call `MCP:query.timeout`. An
  interrupted tool is not an error.
- `cursor-agent -p` fires no prompt, `stop` or subagent hooks. Each run is
  one turn, from its first event to its `sessionEnd`, and a Task subagent
  hangs under its Task call. That call gets no end hook either, so it closes
  with the run and carries `cursor.tool.closed_at_session_end`. A resumed
  chat (`--resume`) adds turns to the same trace.
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
- `cursor-agent -p --resume` fires no `sessionStart`, so a Task subagent
  started in a resumed run cannot be linked to the chat: its Task call
  closes with the run and has nothing under it, and the subagent's own
  events are not sent.

## What gets sent

The rules are the same as for Claude Code. See
[What gets sent](../README.md#what-gets-sent----read-this-before-enabling-anything).
Tracing is off until you enable a folder. `/rius-enable-here` sends
structure only; `/rius-enable-content-here` also sends prompts, answers, and
tool input and output, with secrets removed. `RIUS_CAPTURE_CONTENT=false`
turns content off everywhere.

Each hook appends the event to a local spool at
`~/.cursor/rius/spool/<conversation>.jsonl` (mode 0600). With capture off,
no content is ever written to the spool; with it on, secrets are removed
before content is written, and the output of a call that reads a
secret-shaped file (`.env*`, `*.pem`, ...) is never written. Your email and transcript paths are
never written there. A spool is deleted once its conversation has been idle
for 7 days and its trace is closed; the cleanup runs when a traced chat
starts.

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

`RIUS_API_KEY` and `RIUS_ENDPOINT` in the environment are ignored, as they
are in Claude Code. To trace with a key you minted in the console, store it
from a terminal:

```
pbpaste | bash <plugin>/scripts/rius_ctl.sh use-key --agent cursor   # add --env staging for staging
```

See [API keys](api-keys.md).

| Command | What it does |
|---|---|
| `/rius-login`, `/rius-logout` | Sign in, or sign out and revoke the key |
| `/rius-status` | Whether this folder is traced, and why |
| `/rius-enable-here`, `/rius-disable-here` | Trace this folder and everything under it (structure only), or stop |
| `/rius-enable-content-here` | Trace this folder with content, secrets removed |
| `/rius-on`, `/rius-off` | This chat only |

The commands are prompts that only you start: Cursor never runs a command on
its own, and each also sets `disable-model-invocation: true`. The agent runs
`scripts/rius_ctl.sh` through its Shell tool; Cursor commands have no
per-command tool allow-list, so Cursor may ask you to approve the command. The plugin's
`sessionStart` hook tells the session where the plugin lives
(`RIUS_PLUGIN_ROOT`) and which conversation it is (`RIUS_CURSOR_SESSION_ID`).
Cursor's CLI hands those variables to later hooks but not always to the
Shell tool, so the hook also puts both values in the chat's context, on a
line starting `Rius plugin:`, and the commands use that when the variables
are empty. If a command still cannot find the script, start a new chat or
run the same command from a terminal.

Settings use the `RIUS_CURSOR_` prefix where Claude Code uses
`RIUS_CLAUDE_`: `RIUS_CURSOR_ENABLED`, `RIUS_CURSOR_DEBUG`,
`RIUS_CURSOR_MAX_ATTR_BYTES`. As in Claude Code they can only turn things
off: `RIUS_CURSOR_ENABLED=true` is ignored. Logs go to `~/.cursor/rius/log/`,
under your home folder as the operating system reports it, not `HOME`.

## Querying traces from Cursor (MCP)

`cursor/mcp.json` registers the Rius MCP server
(`https://mcp.eu.console.rius-glassflow.com/mcp`) with no key, so Cursor signs
in with OAuth: use Connect in the MCP settings, or
`cursor-agent mcp login rius`. This sign-in is separate from `/rius-login`.

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
