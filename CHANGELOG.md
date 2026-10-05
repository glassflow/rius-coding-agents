# Changelog

Notable changes to the `rius` Claude Code plugin. Versions follow
[semantic versioning](https://semver.org). The version in
`.claude-plugin/plugin.json` is what the marketplace installs.

## Unreleased

- Each session now starts with one short line saying whether Rius is
  tracing it, and to which workspace, with content on or off. A folder you
  enabled before signing in says to run `/rius:login`. Right after
  install, Rius says once how to get started, then stays quiet until you
  opt in. It is a status line for you, with no instructions for Claude, and
  a resumed or compacted session sees it again only if it changed.
- When the backend refuses data because the trial ended or the workspace is
  locked or paused (HTTP 402), the plugin used to drop it without a word.
  Now the next session and `/rius:status` say so and point you to the
  console. The notice clears after the next successful export.

## 0.4.5 (2026-10-01)

- A session that is killed or crashes, and so never sends SessionEnd, no
  longer stays pending forever and missing from the Agents and Users tabs.
  The next session you start with the same API key closes its trace once
  it has been silent for 12 hours and Claude Code is no longer running.
  The trace ends at the last thing the session did, and the closing spans
  carry no content. A trace opened with a different key, such as a
  project's own `RIUS_API_KEY` for another workspace, is never closed this
  way, because its closing spans would land in the wrong workspace. To
  tell keys apart, the state file keeps a short hash of the key and
  endpoint, never the key. (#21)

## 0.4.4 (2026-10-01)

- On macOS, `/rius:enable-here` in a folder under `/tmp`, in any folder
  reached through a symlink, or in a folder typed in the wrong letter case,
  now works. The rule kept the shell's spelling, `/tmp/proj`, while Claude
  Code gives the hooks the resolved folder, `/private/tmp/proj`. No rule
  matched, so every hook quietly sent nothing, and `/rius:status` still
  said `on` with 0 spans exported. `/rius:enable-here` and
  `/rius:disable-here` now store the resolved folder. Rules are still
  matched as written: a rule never follows a symlink that is created or
  repointed later. A rule an earlier version wrote through a symlink still
  does not match. `/rius:status` names it, and
  running `/rius:enable-here` in that folder again replaces it. Old
  disables are repaired by any rule change, so enabling a parent again
  never traces a folder that was disabled under it.
- `/rius:status` decides for the folder the hooks see and shows it when it
  differs, as in `cwd: /tmp/proj (hooks see /private/tmp/proj)`. When
  tracing is on but no hook has traced the session, it says to restart
  Claude Code. A plugin installed mid-session is not hooked into that
  session.
- A path rule whose fixed part is the filesystem root, such as `/*` or
  `C:\*`, matches nothing, like `/` already did. `/rius:enable-here` and
  `/rius:disable-here` now refuse such a folder instead of saying they
  changed it.

## 0.4.3 (2026-10-01)

- A conversation Claude Code moves to a new session id (sending a session
  to the background does this) stays one trace, like a resumed session.
  The new session's transcript opens with a copy of the whole history, and
  the plugin sent it all again as a second trace: both traces started at
  the same millisecond, every token, cost and error before the move counted
  twice, and the first trace never closed. The new session now skips the
  copied history and carries on in the same trace, under the same root and
  `session.id`, as a new instance. Its turns carry `cc.continued_from`.
  Subagents still running at the move keep landing in the trace, the
  conversation's root closes at the continued session's end, and a
  mid-session disable carries over to the continued session. (#12)
- The trace is titled with the Claude Code session's name instead of
  `claude-code session`: the `/rename` or `--name` title, else the title
  Claude Code generates from the first prompt. A rename mid-session renames
  the trace at the next hook event. With `RIUS_CAPTURE_CONTENT=false` no
  name is sent. The agent stays `claude-code`. (#16)
- No behavior change: a code comment says the tool-error message cap bounds
  size, not content (#13); the bug form says where to find the plugin
  version (#15); and a test pins the late continuation check that keeps a
  stopped conversation from sending content (#17).

## 0.4.2 (2026-09-30)

- `/rius:status` says when a session was stopped by a mid-session disable:
  it reads `Rius tracing: off` with a `Stopped:` line, instead of the `on`
  the folder rules alone would give.
- Each model API response is one generation span. Claude Code writes a
  response as one transcript line per content block, each repeating the
  usage, and the plugin made a span per line: tokens and cost were counted
  once per block, often 2-6 times over. Lines are now grouped by
  `message.id`, the usage is counted once (from the response's last line,
  since streamed subagent lines carry partial counts), and the response's
  tool calls all sit under it.
- Background subagents' model calls are traced. A background subagent's
  `Agent` call returns a launch acknowledgement at once, and the plugin
  stopped reading the subagent's file there, when it held only the brief:
  the subagent showed 0 tokens and no children, and its span ended at the
  acknowledgement. The file is now read for as long as it grows, and the
  subagent's span runs from its first line to its last, not from the
  `Agent` tool_use line, which can be stamped tens of seconds before a
  parallel batch of subagents starts.
- `gen_ai.usage.input_tokens` includes the cache reads and writes, as the
  OTel GenAI conventions, the Rius attribute reference and the Rius SDKs
  define it. Anthropic's own count leaves them out (2 fresh tokens next to
  50k cached is normal), so Rius token totals left out every cached token.
  Thinking tokens are sent as `gen_ai.usage.reasoning.output_tokens`.
- Each generation carries `rius.context.sizes`: the byte sizes of what its
  prompt held (user and assistant history, the current turn, each tool's
  calls and results), estimated from the transcript. The console's Context
  panel showed only "cached prefix" and "fresh input" for Claude Code
  sessions because the plugin sent neither content nor sizes. Sizes are not
  content and are sent with capture off too.
- A failed tool call carries an `exception` event whose type is the tool
  and how it failed (`Bash.exit_1`, `Write.tool_error`) and whose message is
  the one line that says why, such as a traceback's last line. The console
  groups errors by that event. Without it the whole tool output was the
  error's type and message, so an 80-line file piped before a failing
  `grep` became an error type, and each distinct output was its own group.
  The status message is that same line; the full output stays in
  `output.value`.

## 0.4.1 (2026-09-30)

- A session whose folder is disabled mid-session (`/rius:disable-here`,
  `/rius:off`, `RIUS_CLAUDE_ENABLED=false`) still closes its trace when it
  ends, so it is counted on the console's Agents and Users tabs. Nothing
  from after the disable is sent, the closing spans carry no content, and
  re-enabling the folder does not resume that session; a new session does.
- A tool call that never returned (interrupted, or the session ended
  mid-call) is closed at session end instead of staying pending, which kept
  the whole session out of those counts too.
- The README leads with what Rius shows and how to install. Reference
  material moved into `docs/` pages (installing and updating, how it works,
  API keys), and the design records moved to `docs/design/`.

## 0.4.0 (2026-09-30)

Production is the default.

- `/rius:login` signs in to production (`console.rius-glassflow.com`).
  Staging needs `/rius:login --env staging` or `RIUS_ENV=staging`; `--env`
  wins over `RIUS_ENV`.
- An unknown environment name is refused instead of falling back to
  production.
- Polling the sign-in host never runs faster than every 2 seconds, whatever
  interval the server asks for.
- The bundled MCP server points at the production MCP host
  (`mcp.eu.console.rius-glassflow.com`) by default.
- `/rius:status` prints the MCP URL in use. When the stored key belongs to
  another environment, `/rius:status` and the login flow print the
  `RIUS_MCP_URL=` setting that fixes it.
- The bundled MCP server always uses the `/rius:login` key. Claude Code
  withholds credential-named variables from a plugin's headers helper, so
  `RIUS_API_KEY` applies to tracing only. `/rius:status` prints which key
  the MCP server has (`MCP key:`).
- `/rius:enable-here` says what the folder will upload and to which
  workspace (or that it will send once you sign in, when there is no key
  yet), names disabled subfolders it does not reach, and shows how to stop.
- README and getting started cover production hosts, with staging as its
  own section.

## 0.3.0 (2026-09-27)

Renamed from `rius-claude-code` to `rius`. See
[Installing and updating](docs/install.md#upgrading-from-rius-claude-code)
to move over without duplicate hooks.

- One slash command per action: `/rius:login`, `/rius:logout`,
  `/rius:enable-here`, `/rius:disable-here`, `/rius:status`, `/rius:on`,
  `/rius:off`.
- `/rius:login` opens the Rius console in the browser, where you pick a
  workspace and the plugin receives a key for it. It replaces the
  device-code flow from 0.2.0.
- `/rius:logout`, and a re-login, revoke the previous key on the server
  before deleting it locally.
- `/rius:disable-here` turns a folder off. A disabled folder beats any
  enabled parent, and `/rius:enable-here` under a disabled parent says the
  folder is still off.
- Only one login wait runs at a time, and it stays inside its time budget.

## 0.2.1 (2026-09-25)

- The Rius MCP server is bundled as `rius` and authenticates with the key
  from the login command through a headers helper, so the key never lands in
  an MCP config file.
- A 401 from ingest is treated as transient, so spans sent right after
  login are not lost while the new key propagates.
- The login code is shown before the wait starts.
- The slash command passes its arguments through literally.

## 0.2.0 (2026-09-25)

- A login command: sign in with a device code and store a key with
  `ingest` and `read` scopes.

## 0.1.0 (2026-09-22)

First version.

- One OTLP GenAI trace per Claude Code session: turns, generations with
  token counts and cache reads, tool calls with input and output.
- Subagents are read from their own transcript files and nested under the
  tool call that spawned them.
- Spans are sent while the session runs, and a heartbeat marks the session
  alive every 15 seconds.
- Tracing is off until a folder is enabled. `RIUS_CAPTURE_CONTENT=false`
  keeps structure, tokens and timing and drops content.
- macOS, Linux and Windows (through Git Bash), Python 3.9+, standard
  library only.
