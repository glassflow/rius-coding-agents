---
name: rius-status
description: Show whether Rius is tracing this Codex session and folder, and why. Use when the user asks about Rius status or why nothing reaches Rius.
---

# rius-status

This file is `<plugin root>/codex/skills/rius-status/SKILL.md`; the plugin root is
the directory three levels above it. Below, `<plugin root>` means that absolute
path.

Run this command with the shell tool, from the current working directory:

```
bash "<plugin root>/scripts/rius_ctl.sh" status --session "${CODEX_THREAD_ID:-}" --agent codex --cwd "$PWD"
```

Report its output to the user verbatim. Do not add interpretation.
