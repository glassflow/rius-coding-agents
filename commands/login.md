---
description: Sign in to Rius in the browser and pick the workspace this machine sends traces to
allowed-tools: Bash(bash ${CLAUDE_PLUGIN_ROOT}/scripts/rius_ctl.sh:*)
---

!`bash "${CLAUDE_PLUGIN_ROOT}/scripts/rius_ctl.sh" login $ARGUMENTS --cwd "$PWD"`

Report the output above to the user verbatim. Do not add interpretation.

If a line starts with `RIUS_LOGIN_PENDING:`, sign-in is not finished:

1. FIRST, before calling any tool, write a message showing the user the output
   above verbatim, omitting only the `RIUS_LOGIN_PENDING:` line. The `Open:` URL
   and the `Code:` must be visible, because the user needs them now.
2. THEN run the command that follows `RIUS_LOGIN_PENDING:` with the Bash tool,
   exactly as printed, with `run_in_background: true` and a 600000 ms timeout.
   Do not block the user while it runs; carry on with anything else they ask.
3. When it finishes, report its output verbatim, omitting any
   `RIUS_LOGIN_PENDING:` line. If the output says `Still waiting`, run the
   command from its `RIUS_LOGIN_PENDING:` line again the same way, in the
   background, and keep doing so until it prints something else.
4. If it printed `Connected as`, offer to run `/rius:enable-here` for the
   current folder. Do not run it unless the user says yes.
