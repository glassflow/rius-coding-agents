# API keys

`/rius:login` is the normal way to get a key. This page is for minting one
by hand and deciding where it lives.

## Getting a Rius workspace and API key

The plugin cannot do anything without a Rius API key. `/rius:login` gets
you one without leaving Claude Code. To mint one by hand instead, for
`RIUS_API_KEY`, the short version is:

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
  `ingest`. The Rius MCP server needs `read`. One key can hold both.

> **The first key has to come from a browser login.** An API key can never
> mint another API key -- the backend refuses, so that a leaked agent key
> cannot create more credentials. Either the console or a single interactive
> OAuth session against the Rius MCP server gets you the first one; after
> that the MCP server's `create_api_key` can mint further keys, but it
> cannot create a workspace and there is no revoke tool.

The [getting started guide](getting-started.md) has the full version,
including which endpoint to use for which environment.

## Where the API key goes

Put it in the project's `.claude/settings.local.json`, not in the global
`~/.claude/settings.json`:

```json
{
  "env": {
    "RIUS_API_KEY": "ri_xxxxxxxxxxxxxxxx.xxxxxxxxxxxxxxxx"
  }
}
```

`settings.local.json` is the per-project file that is conventionally
gitignored, so the credential does not follow the repo into a commit --
check that your repo does ignore it before writing a key there. A key is
also scoped to a single Rius workspace, so projects reporting into different
workspaces need different keys, which one global value cannot express. And a
key in the global file applies to every folder on the machine; tracing is
off per folder by default, so that is not a leak by itself, but it makes the
blast radius of a later `/rius:enable-here` wider than it needs to be.

Exporting `RIUS_API_KEY` in the shell that launches Claude Code works just
as well. Without it, the plugin uses the key `/rius:login` stored in
`~/.claude/rius/credentials.json` (mode 0600); `RIUS_API_KEY` wins when both
are present.
