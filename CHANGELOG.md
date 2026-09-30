# Changelog

Notable changes to the `rius` Claude Code plugin. Versions follow
[semantic versioning](https://semver.org). The version in
`.claude-plugin/plugin.json` is what the marketplace installs.

## Unreleased

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
