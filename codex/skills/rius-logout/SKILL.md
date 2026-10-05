---
name: rius-logout
description: Sign out of Rius on this machine and revoke the stored key. Use when the user asks to log out of Rius.
---

# rius-logout

This file is `<plugin root>/codex/skills/rius-logout/SKILL.md`; the plugin root is
the directory three levels above it. Below, `<plugin root>` means that absolute
path.

Run this command with the shell tool, from the current working directory:

```
bash "<plugin root>/scripts/rius_ctl.sh" logout --agent codex --cwd "$PWD"
```

The command writes Rius settings under `$CODEX_HOME` (default `~/.codex`), outside the
workspace, so run it with escalated sandbox permissions
(`sandbox_permissions: "require_escalated"`), with the justification "Rius
settings live under ~/.codex/rius".

It also calls the Rius server to revoke the key, so it needs network access too.

Report its output to the user verbatim. Do not add interpretation.
