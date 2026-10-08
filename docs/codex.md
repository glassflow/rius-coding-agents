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

Type `$` and pick a skill. Codex never runs them on its own: each sets
`allow_implicit_invocation: false`, Codex's form of Claude Code's
`disable-model-invocation`, so only you can start one. Codex skills have no
per-skill tool allow-list, so Codex asks before running the command unless
your sandbox already allows it.

| Skill | What it does |
|---|---|
| `$rius:rius-login` | Browser sign-in; you pick the workspace |
| `$rius:rius-enable-here` | Trace this folder and everything under it, structure only |
| `$rius:rius-enable-content-here` | The same, with prompts, replies and tool output (secrets removed) |
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

It prints a URL and a code. Approve in the browser, then run the same
command with `login-wait` in place of `login`.

Rius keeps its Codex key, settings and state in `~/.codex/rius/` under
your home folder as the operating system reports it, even when
`CODEX_HOME` points somewhere else. A repository can set environment
variables for Codex's hooks and commands, so `CODEX_HOME` (like `HOME`)
cannot be allowed to choose where the key is read from or which folders
are traced. Rius still reads `$CODEX_HOME/config.toml`, only to count the
approved hooks in `$rius:rius-status`.

Codex keeps its own key, separate from Claude Code's. Logging out of one does
not sign out the other. `RIUS_API_KEY` and `RIUS_ENDPOINT` in the
environment are ignored, as they are for Claude Code. To trace with a key
you minted in the console, store it from a terminal:

```
pbpaste | bash <plugin>/scripts/rius_ctl.sh use-key --agent codex   # add --env staging for staging
```

See [API keys](api-keys.md).

## What a trace contains

One trace per Codex session (thread). A resumed session (`codex resume`,
`codex exec resume`) continues the same trace.

| Span | From the rollout file |
|---|---|
| AGENT `codex session` | the session; open until the session is closed (below) |
| CHAIN `turn` | one per turn: prompt, final reply, `codex.turn.duration_ms`, `codex.turn.ttft_ms` |
| LLM `<model>` | one per model call: `input_tokens` (includes cached), `cache_read.input_tokens`, `output_tokens`, `reasoning.output_tokens` |
| TOOL `<tool>` | one per call; a command that exits non-zero is `error.type = exec_command.exit_<n>`, an MCP error is `<tool>.tool_error`; a shell call (`exec_command`, or a code-mode script that calls it) carries `rius.command.class` (`test`, `build`, `lint`, `package`, `git`, `other`) and `process.exit.code` |
| AGENT `<role>` | a subagent, under the `spawn_agent` call that started it (`multi_agent_v1`, or the opt-in `multi_agent_v2`), with its own turns and calls |

Resource attributes: `service.name=codex`, `codex.version`, `codex.cwd`,
`codex.originator`. Content rules are the same as for Claude Code:
`$rius:rius-enable-here` sends structure only, `$rius:rius-enable-content-here`
adds content with secrets removed, and `RIUS_CAPTURE_CONTENT=false` turns
content off everywhere.

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

The bundled server takes no key: its entry is just the URL, so Codex
uses OAuth.

## Settings

A repository can set these for Codex, so, as for Claude Code, they can only
turn things off.

| Variable | Effect |
|---|---|
| `RIUS_CODEX_ENABLED` | `false` turns tracing off; `true` is ignored |
| `RIUS_CODEX_DEBUG` | logs to `~/.codex/rius/log/` |
| `RIUS_CODEX_MAX_ATTR_BYTES` | cap on each content attribute (default 32768); can only be lowered |
| `RIUS_API_KEY`, `RIUS_ENDPOINT` | ignored, as for Claude Code |
| `RIUS_CAPTURE_CONTENT`, `RIUS_SERVICE_NAME` | shared with Claude Code |

## Known gaps (beta)

- Until the Rius backend learns the agent name, the key a Codex login mints
  and the connect page still say "Claude Code".
- LLM span start times are approximate (the previous event in the rollout).
- OpenAI does not report cache-write tokens, so none are sent.
- Code mode (`code_mode_host`, on by default in codex-cli 0.144.1) runs
  every tool through one `exec` call that runs a script. Each such call is
  one TOOL span; the commands, patches and spawns inside it get no spans of
  their own. Its output carries no exit code, so a failed command inside
  `exec` reads as success.
  - **Name (best effort).** The span is named after the tools its script
    calls (`exec_command`, `apply_patch`, `mcp__<server>__<tool>`). The
    script is read by a small scanner, not a JavaScript parser: when it
    cannot be read to its end, is over 64 KB, or holds a `/` that could be
    a division or a regular expression with a quote or brace in between, the
    span is named plain `exec`. A name is only ever a tool called as
    `tools.<name>(` in code, never text from a string or a comment.
  - **Secret files (withholds when in doubt).** A script whose text names a
    secret-shaped file anywhere (`.env*`, `*.pem`, `*.key`, `id_rsa*`,
    `credentials*`, ...: the names the other agents use) has its output
    replaced, whether the name is a path, a word of a command, a property
    such as `obj.key`, or a comment. This does not rest on the scanner, so
    it errs towards withholding the output of a harmless script; a script
    over 64 KB is withheld as well. A script the scanner cannot read to its
    end is not withheld for that: it is only named plain `exec`.
  - **Subagents (placement approximate, none dropped while the rollout
    exists).** A subagent spawned inside `exec` goes under the call that was
    running when it was created and whose script can spawn one, else under
    any call that was running, else under the last one begun. While a
    `spawn_agent` call is open its output (or, in `multi_agent_v2`, the
    `sub_agent_activity` line) names the child exactly, so the child waits
    for it. If the session ends first, the child is placed when its trace is
    closed (under an `exec` call as above, else under its spawner's open
    turn, else under the session), and its rollout is read then, unless the
    session was stopped. When several `exec` calls run at once nothing says
    which spawned which: they take their subagents in the order they began,
    among the subagents Rius has heard of so far, and a younger one heard of
    first can take the call that began first.
- Interrupted turns are parsed from their documented shape but were not
  seen in a recorded run.
- A failed tool call is only marked as an error when it is a non-zero exit
  or an MCP error; other failures read as success.
- `codex exec --ephemeral` writes no rollout file, so it is not traced.
