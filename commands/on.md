---
description: Turn Rius tracing on for this session only
allowed-tools: Bash(bash ${CLAUDE_PLUGIN_ROOT}/scripts/rius_ctl.sh on:*)
---

!`bash "${CLAUDE_PLUGIN_ROOT}/scripts/rius_ctl.sh" on --session ${CLAUDE_SESSION_ID} $ARGUMENTS --cwd "$PWD"`

Report the output above to the user verbatim. Do not add interpretation.
