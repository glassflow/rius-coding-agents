# Rius for Codex (beta)

The `rius` plugin also runs in the [Codex](https://developers.openai.com/codex)
CLI. It was built and tested against codex-cli 0.144.1. The Claude Code
plugin is unchanged; Codex reads its own manifest (`.codex-plugin/plugin.json`)
and never loads the Claude Code hooks, commands or MCP config.

## Install

```
codex plugin marketplace add glassflow/rius-coding-agents
codex plugin add rius@rius-coding-agents
```

Codex copies the plugin to
`${CODEX_HOME:-~/.codex}/plugins/cache/rius-coding-agents/rius/<version>/`.

### Trust the hooks

Codex runs no plugin hook until you approve it. Open `/hooks` in Codex and
trust the five `rius` hooks (SessionStart, UserPromptSubmit, PostToolUse,
Stop, SubagentStop). Until then nothing is traced, and nothing says so
except `$rius:rius-status`, whose `Hooks:` line counts the approved ones.

Approval is stored in `config.toml` and survived a plugin upgrade in our
tests. If a future version changes the hooks, Codex asks again.

## Sign in and enable a folder

Ask Codex to run the skills (type `$` to pick one, or ask in plain words):

| Skill | What it does |
|---|---|
| `$rius:rius-login` | Browser sign-in; you pick the workspace |
| `$rius:rius-enable-here` | Trace this folder and everything under it |
| `$rius:rius-disable-here` | Stop tracing this folder |
| `$rius:rius-on` / `$rius:rius-off` | This session only |
| `$rius:rius-status` | What is on, why, the key, the hooks |
| `$rius:rius-logout` | Remove and revoke the stored key |

Login, logout and the enable/disable skills write to `~/.codex/rius/`,
outside the workspace, and login needs the network. In the default sandbox Codex asks you to approve an escalation for
each of them.

If the sandbox will not allow it, run the same command in a terminal:

```
bash ~/.codex/plugins/cache/rius-coding-agents/rius/<version>/scripts/rius_ctl.sh login --agent codex
```

It prints a URL and a code. Approve in the browser, then run the
`login-wait` command it prints.

Rius keeps its Codex key, settings and state in `~/.codex/rius/` under
your home folder as the operating system reports it, even when
`CODEX_HOME` points somewhere else. A repository can set environment
variables for Codex's hooks and commands, so `CODEX_HOME` (like `HOME`)
cannot be allowed to choose where the key is read from or which folders
are traced. Rius still reads `$CODEX_HOME/config.toml`, only to count the
approved hooks in `$rius:rius-status`.

Codex keeps its own key, separate from Claude Code's. Logging out of one does
not sign out the other. `RIUS_API_KEY` in the environment still beats the
stored key.

## What a trace contains

One trace per Codex session (thread). A resumed session (`codex resume`,
`codex exec resume`) continues the same trace.

| Span | From the rollout file |
|---|---|
| AGENT `codex session` | the session; open until the session is closed (below) |
| CHAIN `turn` | one per turn: prompt, final reply, `codex.turn.duration_ms`, `codex.turn.ttft_ms` |
| LLM `<model>` | one per model call: `input_tokens` (includes cached), `cache_read.input_tokens`, `output_tokens`, `reasoning.output_tokens` |
| TOOL `<tool>` | one per call; a command that exits non-zero is `error.type = exec_command.exit_<n>`, an MCP error is `<tool>.tool_error` |
| AGENT `<role>` | a subagent, under the `spawn_agent` call that started it, with its own turns and calls |

Resource attributes: `service.name=codex`, `codex.version`, `codex.cwd`,
`codex.originator`. Content rules are the same as for Claude Code:
`RIUS_CAPTURE_CONTENT=false` sends structure only.

### How a session ends

Codex has no SessionEnd hook. A Codex trace is closed by the next Codex
session you start with the same key, once the old session's Codex process
has exited and it has been idle for an hour. The trace ends at the last
thing the session did. Claude Code's own rule (12 hours) is unchanged.

## Querying traces (MCP)

The plugin registers the Rius MCP server as `rius`. Codex has no way to hand
it the stored key, so it signs in on its own through the server's OAuth:

```
codex mcp login rius
```

Do this once; it is a second sign-in, separate from `$rius:rius-login`.

If you use `RIUS_API_KEY` instead, override the server in `config.toml`.
Codex then sends the key as a bearer token. Note that with this setting and
no `RIUS_API_KEY` in the environment, Codex drops the server silently:

```toml
[mcp_servers.rius]
url = "https://mcp.eu.console.rius-glassflow.com/mcp"
bearer_token_env_var = "RIUS_API_KEY"
```

## Settings

| Variable | Effect |
|---|---|
| `RIUS_CODEX_ENABLED` | `true`/`false` overrides the folder rules |
| `RIUS_CODEX_DEBUG` | logs to `~/.codex/rius/log/` |
| `RIUS_CODEX_MAX_ATTR_BYTES` | cap on each content attribute (default 32768) |
| `RIUS_API_KEY`, `RIUS_ENDPOINT` | same as for Claude Code |
| `RIUS_CAPTURE_CONTENT`, `RIUS_SERVICE_NAME` | shared with Claude Code |

## Known gaps (beta)

- Until the Rius backend learns the agent name, the key a Codex login mints
  and the connect page still say "Claude Code".
- LLM span start times are approximate (the previous event in the rollout).
- OpenAI does not report cache-write tokens, so none are sent.
- `apply_patch` (`custom_tool_call`) and interrupted turns are parsed from
  their documented shape but were not seen in a recorded run.
- A failed tool call is only marked as an error when it is a non-zero exit
  or an MCP error; other failures read as success.
- `codex exec --ephemeral` writes no rollout file, so it is not traced.
