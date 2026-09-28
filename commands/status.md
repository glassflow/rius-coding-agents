---
description: Show whether Rius is tracing this folder, and why
allowed-tools: Bash(bash ${CLAUDE_PLUGIN_ROOT}/scripts/rius_ctl.sh:*)
---

!`bash "${CLAUDE_PLUGIN_ROOT}/scripts/rius_ctl.sh" status --session ${CLAUDE_SESSION_ID} $ARGUMENTS --cwd "$PWD"`

Report the output above to the user verbatim. Do not add interpretation.
