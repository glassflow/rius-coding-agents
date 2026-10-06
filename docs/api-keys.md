# API keys

`/rius:login` is how the plugin gets its key. This page is for minting one
by hand, for the MCP server or the SDKs.

## Getting a Rius workspace and API key

The plugin cannot do anything without a Rius API key, and `/rius:login` is
how it gets one. To mint one by hand, for the MCP server or the SDKs, the
short version is:

- Log in to the Rius console (`https://console.rius-glassflow.com`) in a
  browser. A `Default` workspace is created
  for an identity that has none the first time it lists workspaces, so for
  most people that is the whole workspace step. Creating further workspaces
  explicitly is restricted to organization admins.
- Mint a key in workspace settings. A key looks like `ri_<id>.<signature>`;
  older `gf_` keys still work. The plaintext is shown once and stored
  hashed. Expiry is chosen at creation from never, 30 days, 90 days or 1
  year, defaulting to never.
- Keys are scoped to one workspace and carry scopes. This plugin needs
  `ingest`. The Rius MCP server needs `read` when you give it a key; the
  plugin's bundled server signs in with OAuth instead. One key can hold both.

> **The first key has to come from a browser login.** An API key can never
> mint another API key -- the backend refuses, so that a leaked agent key
> cannot create more credentials. Either the console or a single interactive
> OAuth session against the Rius MCP server gets you the first one; after
> that the MCP server's `create_api_key` can mint further keys, but it
> cannot create a workspace and there is no revoke tool.

The [getting started guide](getting-started.md) has the full version,
including which endpoint to use for which environment.

## Where the API key goes

Into `~/.claude/rius/credentials.json` (mode 0600), the same file
`/rius:login` writes. To use a key you minted in the console, pipe it in
from a terminal:

```bash
pbpaste | bash <plugin>/scripts/rius_ctl.sh use-key                # production
pbpaste | bash <plugin>/scripts/rius_ctl.sh use-key --env staging
```

`<plugin>` is the installed plugin's folder, for example
`~/.claude/plugins/cache/rius-coding-agents/rius/<version>`.

These commands store the key for Claude Code. For Codex or Cursor add
`--agent codex` or `--agent cursor`: the key then goes into that agent's own
`rius/credentials.json` (`~/.codex/rius/`, `~/.cursor/rius/`) and does not
touch Claude Code's.

The key is read from stdin, so it never lands in your shell history or a
command line. The endpoint is the environment's built-in ingest host and
cannot be given. Storing a key replaces, and revokes, the one stored
before. There is no slash command for it: Claude cannot store a key for
you. `/rius:status` shows `Key from: rius_ctl.sh use-key`.

The plugin traces only with that stored key, and sends it only to the
server stored with it. `RIUS_API_KEY` and `RIUS_ENDPOINT` in the
environment are ignored: Claude Code hands hooks the `env` block of a
project's committed `.claude/settings.json`, so honouring them would let any
repo you clone send your sessions to its own workspace, or your key to its
own server. `/rius:status` says so when either is set. No key reaches the
bundled MCP server either: it signs in with OAuth in `/mcp`.

A key you mint by hand also works for registering the Rius MCP server yourself, as
in
[Exploring your traces](getting-started.md#exploring-your-traces-from-claude-code),
or for the Rius SDKs. Keep it out of any settings file that is committed.
