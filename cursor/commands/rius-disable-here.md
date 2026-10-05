---
name: rius-disable-here
description: Stop tracing this folder and everything under it with Rius
disable-model-invocation: true
---

Run this command with the Shell tool, exactly as written, from the workspace root:

```sh
bash "${RIUS_PLUGIN_ROOT:-${CURSOR_PLUGIN_ROOT}}/scripts/rius_ctl.sh" disable-here --agent cursor --cwd "$PWD"
```

Report its output to the user verbatim. Do not add interpretation.

Cursor may not pass `RIUS_PLUGIN_ROOT` and `RIUS_CURSOR_SESSION_ID` to the
shell. If either is empty there, this chat's context has a line starting
`Rius plugin:` that gives the plugin root and the session id: run the same
command with those values written in place of the variables. If there is no
such line, tell the user to start a new chat, or to run the command from a
terminal with the path printed by `find ~/.cursor/plugins -name rius_ctl.sh`.
