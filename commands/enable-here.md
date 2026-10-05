---
description: Trace this folder and everything under it with Rius (structure only unless you add --with-content)
argument-hint: "[--with-content]"
disable-model-invocation: true
allowed-tools: Bash(bash ${CLAUDE_PLUGIN_ROOT}/scripts/rius_ctl.sh:*)
---

!`bash "${CLAUDE_PLUGIN_ROOT}/scripts/rius_ctl.sh" enable-here $ARGUMENTS --cwd "$PWD"`

Report the output above to the user verbatim. Do not add interpretation.
Whether this folder sends content is the user's choice alone: never run
`/rius:enable-here --with-content` or its script yourself, and do not offer to.
