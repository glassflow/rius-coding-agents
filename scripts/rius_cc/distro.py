"""The distribution that exported a span: this plugin, and its version.

Stamped on every resource as telemetry.distro.name / telemetry.distro.version.
The Rius backend reads process.working_directory and vcs.ref.head.name as a
coding session's repo and branch only on a resource carrying this name: both
are general OpenTelemetry keys any process may send.
"""

from __future__ import annotations

import functools
import json
import os
from typing import Dict

from . import _semconv_generated as semconv

NAME = semconv.TELEMETRY_DISTRO_NAME_CODING_AGENTS

# The plugin's root, two levels above this file (scripts/rius_cc/).
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# Each agent installs the same checkout; the three manifests carry one version.
_MANIFESTS = (".claude-plugin", ".codex-plugin", ".cursor-plugin")


@functools.lru_cache(maxsize=1)
def version() -> str:
    """The version in the first plugin manifest found, or "" when none is."""
    for folder in _MANIFESTS:
        try:
            with open(os.path.join(_ROOT, folder, "plugin.json"), encoding="utf-8") as f:
                value = json.load(f).get("version")
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(value, str) and value:
            return value
    return ""


def resource_attributes() -> Dict[str, str]:
    """telemetry.distro.name, and telemetry.distro.version when known."""
    attrs = {semconv.TELEMETRY_DISTRO_NAME: NAME}
    if version():
        attrs[semconv.TELEMETRY_DISTRO_VERSION] = version()
    return attrs
