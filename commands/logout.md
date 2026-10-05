---
description: Sign out of Rius and revoke this machine's key
disable-model-invocation: true
allowed-tools: Bash(bash "${CLAUDE_PLUGIN_ROOT}/scripts/rius_ctl.sh" logout:*)
---

!`bash "${CLAUDE_PLUGIN_ROOT}/scripts/rius_ctl.sh" logout --cwd "$PWD"`

Report the output above to the user verbatim. Do not add interpretation.
