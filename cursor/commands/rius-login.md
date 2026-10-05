---
name: rius-login
description: Sign in to Rius in the browser and pick the workspace Cursor sends traces to
disable-model-invocation: true
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
2. THEN run this command with the Shell tool, exactly as written. It waits
   up to nine minutes for the browser.

   ```sh
   bash "${RIUS_PLUGIN_ROOT:-${CURSOR_PLUGIN_ROOT}}/scripts/rius_ctl.sh" login-wait --agent cursor --cwd "$PWD"
   ```

3. When it finishes, report its output verbatim, omitting any
   `RIUS_LOGIN_PENDING:` line. If the output says `Still waiting`, run the
   same command again, and keep doing so until it prints something else.
4. If it printed `Connected as`, tell the user they can run
   `/rius-enable-here` in the folder they want traced. It is theirs to run:
   it chooses what that folder sends, so do not run it yourself.

Cursor may not pass `RIUS_PLUGIN_ROOT` and `RIUS_CURSOR_SESSION_ID` to the
shell. If either is empty there, this chat's context has a line starting
`Rius plugin:` that gives the plugin root and the session id: run the same
command with those values written in place of the variables. If there is no
such line, tell the user to start a new chat, or to run the command from a
terminal with the path printed by `find ~/.cursor/plugins -name rius_ctl.sh`.
