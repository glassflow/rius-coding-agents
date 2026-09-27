---
description: Trace this folder and everything under it with Rius
allowed-tools: Bash(bash ${CLAUDE_PLUGIN_ROOT}/scripts/rius_ctl.sh:*)
---

!`bash "${CLAUDE_PLUGIN_ROOT}/scripts/rius_ctl.sh" enable-here $ARGUMENTS --cwd "$PWD"`

Report the output above to the user verbatim. Do not add interpretation.
