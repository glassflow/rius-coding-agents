---
name: rius-off
description: Turn Rius tracing off for this Codex session only. Use when the user asks to stop tracing this session.
---

# rius-off

This file is `<plugin root>/codex/skills/rius-off/SKILL.md`; the plugin root is
that path with `/codex/skills/rius-off/SKILL.md` removed from its end, so
`/x/rius/0.6.0/codex/skills/rius-off/SKILL.md` gives `/x/rius/0.6.0`.
Below, `<plugin root>` means that absolute path.

Run this command with the shell tool, from the current working directory:

```
bash "<plugin root>/scripts/rius_ctl.sh" off --session "${CODEX_THREAD_ID:-}" --agent codex --cwd "$PWD"
```

The command writes Rius settings under `~/.codex/rius`, outside the
workspace, so run it with escalated sandbox permissions
(`sandbox_permissions: "require_escalated"`), with the justification "Rius
settings live under ~/.codex/rius".

Report its output to the user verbatim. Do not add interpretation.
