---
name: rius-login
description: Sign in to Rius in the browser and pick the workspace Cursor sends traces to
---

Run this command with the Shell tool, exactly as written, from the workspace root:

```sh
bash "${RIUS_PLUGIN_ROOT:-${CURSOR_PLUGIN_ROOT}}/scripts/rius_ctl.sh" login --agent cursor --cwd "$PWD"
```

Report its output to the user verbatim. Do not add interpretation.

If a line starts with `RIUS_LOGIN_PENDING:`, sign-in is not finished:

1. FIRST, before running anything else, show the user the output verbatim,
   omitting only the `RIUS_LOGIN_PENDING:` line. The `Open:` URL and the
   `Code:` must be visible, because the user needs them now.
2. THEN run the command that follows `RIUS_LOGIN_PENDING:` with the Shell
   tool, exactly as printed. It waits up to nine minutes for the browser.
3. When it finishes, report its output verbatim, omitting any
   `RIUS_LOGIN_PENDING:` line. If the output says `Still waiting`, run the
   command from its `RIUS_LOGIN_PENDING:` line again, and keep doing so until
   it prints something else.
4. If it printed `Connected as`, offer to run `/rius-enable-here` for the
   current folder. Do not run it unless the user says yes.

Cursor may not pass `RIUS_PLUGIN_ROOT` and `RIUS_CURSOR_SESSION_ID` to the
shell. If either is empty there, this chat's context has a line starting
`Rius plugin:` that gives the plugin root and the session id: run the same
command with those values written in place of the variables. If there is no
such line, tell the user to start a new chat, or to run the command from a
terminal with the path printed by `find ~/.cursor/plugins -name rius_ctl.sh`.
