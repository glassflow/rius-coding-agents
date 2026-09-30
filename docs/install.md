# Installing and updating

The short version is in the [README](../README.md#quick-start). This page covers
updates, the old plugin name, working from a local checkout, and platforms.

## From GitHub

```
/plugin marketplace add glassflow/rius-coding-agents
/plugin install rius@rius-coding-agents
```

The repo is public, so no GitHub credentials are needed.

## Updating an installed plugin

> **Claude Code caches the marketplace. Refresh it after every plugin
> change.** A marketplace added from a directory or a git source is read
> once and cached, not read live, so an updated plugin -- upstream or in
> your own checkout -- does not reach your session until you run:
>
> ```
> /plugin marketplace update rius-coding-agents
> /reload-plugins
> ```
>
> Skipping this is not a cosmetic problem. A stale cache is indistinguishable
> from a change that did not work: the code on disk is new, the code being
> run is old, and nothing says so. It invalidated a full round of testing
> during development of this plugin, which is why it has a section of its
> own rather than a footnote.

## Upgrading from `rius-claude-code`

Versions before 0.3.0 installed the plugin as `rius-claude-code`. Remove it
before installing `rius`, or both stay installed: every hook fires twice
(duplicate spans) and every command appears under both names.

```
/plugin uninstall rius-claude-code@rius-coding-agents
/plugin marketplace update rius-coding-agents
/plugin install rius@rius-coding-agents
```

Your path rules and stored key live under `~/.claude/rius/` and carry over.

## Local development

```
/plugin marketplace add /path/to/rius-coding-agents
/plugin install rius@rius-coding-agents
```

The marketplace name is `rius-coding-agents` and the plugin name is
`rius`, as declared in `.claude-plugin/marketplace.json`. There is
no file watcher: after editing anything under `hooks/` or `scripts/`, run
`/reload-plugins` for the change to take effect in your current Claude Code
process.

## Platform support

macOS, Linux and Windows. The plugin is pure standard library, so the only
requirement is a Python 3.9+ on `PATH`.

On Windows the hook is launched through Git Bash, which Claude Code already
needs for its own Bash tool, and `scripts/hook.sh` picks the interpreter --
`py`, then `python`, then `python3`. If it cannot find one it writes a line
saying so to `~/.claude/rius/log/bootstrap.log` rather than doing nothing
quietly. `/rius:status` prints which platform implementation is live.

If Claude Code on your machine falls back to PowerShell because Git Bash is
not installed, the hooks will not run. That is the one configuration this
plugin does not yet cover.

CI runs the suite on Linux only, across Python 3.10 to 3.13 plus a 3.9
runtime-floor job. There is no Windows CI job yet (tracked as RIUS-945), so
the Windows code paths are covered by unit tests with fakes rather than by a
native run.
