---
description: Sign in to Rius in the browser and pick the workspace this machine sends traces to
argument-hint: "[--env staging]"
disable-model-invocation: true
allowed-tools:
  - Bash(bash "${CLAUDE_PLUGIN_ROOT}/scripts/rius_ctl.sh" login:*)
  - Bash(bash "${CLAUDE_PLUGIN_ROOT}/scripts/rius_ctl.sh" login-wait)
---

!`bash "${CLAUDE_PLUGIN_ROOT}/scripts/rius_ctl.sh" login --cwd "$PWD" -- '$ARGUMENTS'`

Report the output above to the user verbatim. Do not add interpretation.

If a line starts with `RIUS_LOGIN_PENDING:`, sign-in is not finished:

1. FIRST, before calling any tool, write a message showing the user the output
   above verbatim, omitting only the `RIUS_LOGIN_PENDING:` line. The `Open:` URL
   and the `Code:` must be visible, because the user needs them now.
2. THEN run exactly this command with the Bash tool, with
   `run_in_background: true` and a 600000 ms timeout:

   `bash "${CLAUDE_PLUGIN_ROOT}/scripts/rius_ctl.sh" login-wait`

   Do not block the user while it runs; carry on with anything else they ask.
3. When it finishes, report its output verbatim, omitting any
   `RIUS_LOGIN_PENDING:` line. If the output says `Still waiting`, run the
   same command again the same way, in the background, and keep doing so
   until it prints something else.
4. If it printed `Connected as`, tell the user they can run
   `/rius:enable-here` in the folder they want traced. It is theirs to run:
   it chooses what that folder sends, so do not run it yourself.
