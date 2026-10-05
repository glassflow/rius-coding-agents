---
description: Turn Rius tracing off for this session only
disable-model-invocation: true
allowed-tools: Bash(bash "${CLAUDE_PLUGIN_ROOT}/scripts/rius_ctl.sh" off:*)
---

!`bash "${CLAUDE_PLUGIN_ROOT}/scripts/rius_ctl.sh" off --session ${CLAUDE_SESSION_ID} --cwd "$PWD"`

Report the output above to the user verbatim. Do not add interpretation.
