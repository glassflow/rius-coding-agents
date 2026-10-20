"""The attribute keys come from rius-semconv's generated module, vendored at
scripts/rius_cc/_semconv_generated.py, not from hand-typed strings. The guard
below catches a literal equal to a whole registered key; a key built by
concatenation or an f-string gets past it, so don't build keys.

A key typed by hand drifts silently when the registry renames it, which is how
the plugin came to send glassflow.span.pending after the registry retired it.
"""
import io
import json
import pathlib
import re
import tokenize

from rius_cc import _semconv_generated as semconv
from rius_cc import distro, spans

SCRIPTS = pathlib.Path(__file__).resolve().parent.parent / "scripts"
VENDORED = SCRIPTS / "rius_cc" / "_semconv_generated.py"


def _registered_keys():
    return {value for name, value in vars(semconv).items()
            if name.isupper() and isinstance(value, str) and "." in value}


def _shipped_modules():
    return [p for p in sorted(SCRIPTS.rglob("*.py")) if p != VENDORED]


def test_no_shipped_module_types_a_registered_key_by_hand():
    registered = _registered_keys()
    typed = []
    for path in _shipped_modules():
        source = path.read_text(encoding="utf-8")
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type != tokenize.STRING:
                continue
            m = re.fullmatch(r"""(['"])([^'"\\]*)\1""", tok.string)
            if m and m.group(2) in registered:
                typed.append("%s:%d %s" % (path.relative_to(SCRIPTS), tok.start[0], tok.string))
    assert not typed, "use the semconv constant instead of:\n" + "\n".join(typed)


def test_the_pending_allowlist_holds_only_registered_keys():
    unregistered = set(spans._PENDING_ALLOWED_KEYS) - _registered_keys()
    assert not unregistered


def test_the_vendored_module_names_its_release_and_imports_nothing():
    lines = VENDORED.read_text(encoding="utf-8").splitlines()
    assert re.match(r"# Vendored from rius-semconv v\d+\.\d+\.\d+ ", lines[0])
    imports = [line for line in lines if re.match(r"(from|import) ", line)]
    assert imports == ["from __future__ import annotations", "from enum import Enum"]


def test_retired_spellings_are_not_sent():
    assert semconv.RIUS_SPAN_PENDING == "rius.span.pending"
    assert semconv.TRACER_NAME == "rius"


def test_the_distro_marker_names_the_plugin_and_its_manifest_version(tmp_path):
    attrs = distro.resource_attributes()
    assert attrs[semconv.TELEMETRY_DISTRO_NAME] == "rius-coding-agents"
    manifest = SCRIPTS.parent / ".claude-plugin" / "plugin.json"
    assert attrs[semconv.TELEMETRY_DISTRO_VERSION] == json.loads(manifest.read_text())["version"]


def test_no_manifest_means_no_version(monkeypatch, tmp_path):
    monkeypatch.setattr(distro, "_ROOT", str(tmp_path))
    distro.version.cache_clear()
    try:
        assert distro.resource_attributes() == {
            semconv.TELEMETRY_DISTRO_NAME: "rius-coding-agents"}
    finally:
        distro.version.cache_clear()
