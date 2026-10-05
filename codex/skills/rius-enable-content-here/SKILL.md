---
name: rius-enable-content-here
description: Trace this folder with Rius including prompts, file contents and command output, with secrets removed. Only when the user invokes it by name.
---

# rius-enable-content-here

This file is `<plugin root>/codex/skills/rius-enable-content-here/SKILL.md`; the plugin root is
that path with `/codex/skills/rius-enable-content-here/SKILL.md` removed from its end, so
`/x/rius/0.6.0/codex/skills/rius-enable-content-here/SKILL.md` gives `/x/rius/0.6.0`.
Below, `<plugin root>` means that absolute path.

Run this command with the shell tool, from the current working directory:

```
bash "<plugin root>/scripts/rius_ctl.sh" content-on-here --agent codex --cwd "$PWD"
```

The command writes Rius settings under `~/.codex/rius`, outside the
workspace, so run it with escalated sandbox permissions
(`sandbox_permissions: "require_escalated"`), with the justification "Rius
settings live under ~/.codex/rius".

Report its output to the user verbatim. Do not add interpretation.
