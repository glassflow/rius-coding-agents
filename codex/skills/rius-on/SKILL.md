---
name: rius-on
description: Turn Rius tracing on for this Codex session only. Use when the user asks to trace just this session.
---

# rius-on

This file is `<plugin root>/codex/skills/rius-on/SKILL.md`; the plugin root is
the directory three levels above it. Below, `<plugin root>` means that absolute
path.

Run this command with the shell tool, from the current working directory:

```
bash "<plugin root>/scripts/rius_ctl.sh" on --session "${CODEX_THREAD_ID:-}" --agent codex --cwd "$PWD"
```

The command writes Rius settings under `~/.codex/rius`, outside the
workspace, so run it with escalated sandbox permissions
(`sandbox_permissions: "require_escalated"`), with the justification "Rius
settings live under ~/.codex/rius".

Report its output to the user verbatim. Do not add interpretation.
