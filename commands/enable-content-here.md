---
description: Trace this folder with Rius including prompts, file contents and command output, with secrets removed
disable-model-invocation: true
allowed-tools: Bash(bash "${CLAUDE_PLUGIN_ROOT}/scripts/rius_ctl.sh" content-on-here:*)
---

!`bash "${CLAUDE_PLUGIN_ROOT}/scripts/rius_ctl.sh" content-on-here --cwd "$PWD"`

Report the output above to the user verbatim. Do not add interpretation.
