---
name: rius-enable-here
description: Trace this folder and everything under it with Rius. Use when the user asks to enable Rius tracing here.
---

# rius-enable-here

This file is `<plugin root>/codex/skills/rius-enable-here/SKILL.md`; the plugin root is
the directory three levels above it. Below, `<plugin root>` means that absolute
path.

Run this command with the shell tool, from the current working directory:

```
bash "<plugin root>/scripts/rius_ctl.sh" enable-here --agent codex --cwd "$PWD"
```

The command writes Rius settings under `$CODEX_HOME` (default `~/.codex`), outside the
workspace, so run it with escalated sandbox permissions
(`sandbox_permissions: "require_escalated"`), with the justification "Rius
settings live under ~/.codex/rius".

Report its output to the user verbatim. Do not add interpretation.
