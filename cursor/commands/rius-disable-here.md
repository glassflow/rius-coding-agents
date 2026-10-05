---
name: rius-disable-here
description: Stop tracing this folder and everything under it with Rius
---

Run this command with the Shell tool, exactly as written, from the workspace root:

```sh
bash "${RIUS_PLUGIN_ROOT:-${CURSOR_PLUGIN_ROOT}}/scripts/rius_ctl.sh" disable-here --agent cursor --cwd "$PWD"
```

Report its output to the user verbatim. Do not add interpretation.

If the shell reports that `/scripts/rius_ctl.sh` does not exist, the plugin
location is not known in this session. Tell the user to start a new chat, or
to run the same command from a terminal with the path printed by
`find ~/.cursor/plugins -name rius_ctl.sh`.
