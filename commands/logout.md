---
description: Sign out of Rius and revoke this machine's key
allowed-tools: Bash(bash ${CLAUDE_PLUGIN_ROOT}/scripts/rius_ctl.sh logout:*)
---

!`bash "${CLAUDE_PLUGIN_ROOT}/scripts/rius_ctl.sh" logout $ARGUMENTS --cwd "$PWD"`

Report the output above to the user verbatim. Do not add interpretation.
