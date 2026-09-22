---
description: Control Rius tracing for this Claude Code session
allowed-tools: Bash(${CLAUDE_PLUGIN_ROOT}/scripts/rius_ctl.py:*)
---

!`"${CLAUDE_PLUGIN_ROOT}/scripts/rius_ctl.py" ${ARGUMENTS:-status} --cwd "$PWD"`

Report the output above to the user verbatim. Do not add interpretation.
