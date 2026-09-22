---
description: Control Rius tracing for this Claude Code session
allowed-tools: Bash(bash ${CLAUDE_PLUGIN_ROOT}/scripts/rius_ctl.sh:*)
---

!`bash "${CLAUDE_PLUGIN_ROOT}/scripts/rius_ctl.sh" ${ARGUMENTS:-status} --cwd "$PWD"`

Report the output above to the user verbatim. Do not add interpretation.
