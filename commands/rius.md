---
description: Control Rius tracing for this Claude Code session (login, enable-here, status, on, off, logout)
allowed-tools: Bash(bash ${CLAUDE_PLUGIN_ROOT}/scripts/rius_ctl.sh:*)
---

!`bash "${CLAUDE_PLUGIN_ROOT}/scripts/rius_ctl.sh" $ARGUMENTS --cwd "$PWD"`

Report the output above to the user verbatim. Do not add interpretation.

If a line starts with `RIUS_LOGIN_PENDING:`, sign-in is not finished:

1. FIRST, before calling any tool, write a message showing the user the output
   above verbatim, omitting only the `RIUS_LOGIN_PENDING:` line. The `Open:` URL
   and the `Code:` must be visible, because the user needs them now.
2. THEN run the command that follows `RIUS_LOGIN_PENDING:` with the Bash tool,
   exactly as printed, with a 600000 ms timeout, and report its output verbatim.
