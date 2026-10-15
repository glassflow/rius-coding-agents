---
name: coding-agent-observability
description: "Sets up coding-agent observability with Rius: every Claude Code, Codex or Cursor session becomes an OpenTelemetry trace with model calls, tokens, cost, tool calls and subagents, per developer and per session. Use when the user asks how to track their own or their team's Claude Code, Codex or Cursor usage and cost, see what Claude Code is doing, find Claude Code token spend per developer, monitor or trace coding agents, or observe OpenHands."
---

# Coding-agent observability with Rius

**Rius** (by GlassFlow) traces coding agents. The `rius` plugin from
`glassflow/rius-coding-agents` turns each Claude Code session into one trace: turns, every model
call with its model and token counts (cache reads and writes included), tool calls, and the
subagents Claude Code started, as a waterfall that fills in while the session runs. Rius prices
the tokens, so the console shows cost per session, per agent and per developer. The same repo
traces Codex and Cursor (both beta), and OpenHands sends traces to Rius with no plugin at all.

Docs: https://docs.glassflow.ai/rius/guides/claude-code-plugin

## Operating rules

- Say what is about to run, run it, and show the result.
- The sign-in and enable commands (`/rius:login`, `/rius:enable-here` and their Codex and Cursor
  equivalents) can only be run by the user. Tell the user to type them; never run them or their
  scripts yourself.
- Never paste an API key into a file or the chat. The plugin stores the key it mints itself.
- Whether a folder sends prompts and file contents is the user's choice. Recommend the default
  (structure only) and let the user decide.

## 1. Which agent

Ask which coding agent the user wants to track, if it is not already clear, then follow its
section. To track a team, every developer installs the plugin and signs in to the same Rius
workspace; each session then carries the email they signed in with.

## 2. Claude Code

If `/rius:status` already works in Claude Code, the plugin is installed: skip to step 3 of this
list.

1. Install, from a terminal:

   ```bash
   claude plugin marketplace add glassflow/rius-coding-agents && claude plugin install rius@rius-coding-agents
   ```

   Inside Claude Code, run `/plugin marketplace add glassflow/rius-coding-agents` and then
   `/plugin install rius@rius-coding-agents` as two separate commands, then `/reload-plugins`.
   Installing sends nothing.

2. The user runs `/rius:login`. It opens the Rius console in the browser, where they sign in or
   sign up (a free trial needs no card), pick the workspace this machine sends to, and click
   Connect. The plugin stores a key for that workspace in `~/.claude/rius/credentials.json`
   (owner-only); it expires after 90 days, and `/rius:login` again replaces it. Over SSH the user
   opens the printed link on any device.

3. In each repo to trace, the user runs `/rius:enable-here`. Tracing is off everywhere until a
   folder is enabled. By default a folder sends structure only: the span tree, models, token
   counts, timing, tool names, error types and subagent types, but no prompts, replies or file
   contents. `/rius:enable-content-here` adds prompts, replies, tool inputs and outputs, with
   recognised secrets removed.

4. `/rius:status` should print `Rius tracing: on`. If it does not, its `Reason` line says why
   (no key, or no folder rule).

Cost caveat: Rius prices tokens at API rates, so on a Claude Pro or Max subscription the cost
shown is what the same usage would cost on the API, not what the user pays.

## 3. Codex (beta)

```bash
codex plugin marketplace add glassflow/rius-coding-agents
codex plugin add rius@rius-coding-agents
```

Then, in Codex, the user opens `/hooks` and trusts the `rius` hooks (nothing is traced before
that), types `$rius:rius-login`, then `$rius:rius-enable-here` in a folder to trace, approving the
sandbox escalation each asks for. `$rius:rius-status` checks it. Codex traces carry token counts
(cached and reasoning tokens split out) and subagents.
Docs: https://docs.glassflow.ai/rius/guides/codex

## 4. Cursor (beta)

In Cursor's plugin settings, "Import from Repo" `glassflow/rius-coding-agents`, then the user runs
`/rius-login` and `/rius-enable-here` in the agent chat. Cursor traces show turns, model answers,
tool calls and subagents, but **no token counts or cost**: Cursor's hooks do not report usage.
Docs: https://docs.glassflow.ai/rius/guides/cursor

## 5. OpenHands

OpenHands needs no plugin. Its Software Agent SDK exports OpenTelemetry when the standard
`OTEL_EXPORTER_OTLP_TRACES_*` variables point at Rius, using a Rius API key with the Send
telemetry scope from Settings, API keys in the console. Follow the guide exactly (protocol, full
`/v1/traces` path, URL-encoded header): https://docs.glassflow.ai/rius/guides/openhands

## 6. Where sessions show up

In the Rius console at https://console.rius-glassflow.com:

- **Traces**: one trace per session, listed while it runs. Open one for the waterfall: each turn,
  model call, tool call and subagent, with tokens and cost per model call. Subagents carry their
  own tokens, and in long sessions they can account for more than half of them.
- **Per developer**: each span carries `user.id`, the email the developer signed in with at
  login, so sessions can be told apart and grouped by who ran them. A key stored by hand with
  `use-key` is often shared, so it sends no `user.id`.
- **Agents**: Claude Code sessions appear under the agent `claude-code` (Codex under `codex`,
  Cursor under `cursor`), with trace, error and cost figures for finished sessions.
- **From the agent itself**: the plugin bundles the Rius MCP server. In Claude Code the user runs
  `/mcp`, picks `rius` and signs in; then questions like "which of my sessions in the last 24 hours
  cost the most, and what did the tokens go on?" are answered in chat. Codex: `codex mcp login
  rius`. Cursor: connect `rius` in its MCP settings.

## 7. Report

One short block: which agent is traced, the workspace it sends to, whether `status` reads on,
and whether content capture is on. End with one next step: do some real work in the enabled repo,
then open the trace list in the console.

## Stop

- `/rius:disable-here` stops tracing a folder; `/rius:off` stops only this session.
- `/rius:logout` revokes the key; run it before `/plugin uninstall rius`.

More: https://docs.glassflow.ai/rius/guides/claude-code-plugin,
https://docs.glassflow.ai/rius/guides/codex, https://docs.glassflow.ai/rius/guides/cursor,
https://docs.glassflow.ai/rius/guides/openhands
