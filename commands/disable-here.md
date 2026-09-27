---
description: Stop tracing this folder and everything under it with Rius
allowed-tools: Bash(bash ${CLAUDE_PLUGIN_ROOT}/scripts/rius_ctl.sh:*)
---

!`bash "${CLAUDE_PLUGIN_ROOT}/scripts/rius_ctl.sh" disable-here $ARGUMENTS --cwd "$PWD"`

Report the output above to the user verbatim. Do not add interpretation.
