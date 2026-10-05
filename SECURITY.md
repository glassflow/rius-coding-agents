# Security

This plugin handles a Rius API key and, when content capture is on, the
prompts, file contents and command output of the folders you enable. We
take reports about either seriously.

## Reporting a vulnerability

Please don't open a public issue. Report it privately instead:

- **GitHub:** [open a private security advisory](https://github.com/glassflow/rius-coding-agents/security/advisories/new)
  on this repository, or
- **Email:** [help@glassflow.ai](mailto:help@glassflow.ai) with "Security"
  in the subject.

Include what you found, how to reproduce it, the plugin version (from
`.claude-plugin/plugin.json` or `/plugin`), and your OS and Claude Code
version. If a report needs a trace, a log or `/rius:status` output, redact
API keys and anything your sessions captured before sending it.

We'll confirm we have the report, keep you posted while we work on it, and
credit you in the release notes if you'd like.

## What's in scope

- Code in this repository: the hooks, exporter, heartbeat, login flow and
  the bundled MCP configuration.
- Anything that sends data somewhere other than the endpoints the
  [README](README.md#privacy-and-security-posture) lists, sends content
  with `RIUS_CAPTURE_CONTENT=false`, sends anything for a folder that isn't
  enabled, writes the API key to a log or to Claude Code's settings, or
  lets a project's environment turn tracing on, raise content capture, or
  choose the key or the server it is sent to.

Issues in the Rius service itself (the console, ingest or the MCP server)
are welcome through the same channels.

## Supported versions

Fixes go into the latest version only. The marketplace installs the latest,
so update with `/plugin marketplace update rius-coding-agents`.
