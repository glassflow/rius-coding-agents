"""Remove secrets from content before it is exported.

Runs only when a folder sends content. One table, one combined regex, one
pass per value. Every pattern starts at a literal and repeats only bounded
character classes, so a 32 KB value costs a single linear scan: nothing
here can backtrack catastrophically.

A false positive hides a harmless value in a trace; a false negative ships
a credential. The patterns lean towards the first.
"""
from __future__ import annotations

import fnmatch
import json
import re
from typing import Optional

# (name, pattern). The name is what the marker says. Order matters where
# one format is a prefix of another: Anthropic's sk-ant- before OpenAI's sk-.
_PATTERNS = (
    ("private-key", r"-----BEGIN[A-Z ]{0,40}PRIVATE KEY-----"
                    r"[\s\S]*?(?:-----END[A-Z ]{0,40}PRIVATE KEY-----|\Z)"),
    ("authorization", r"(?i:authorization)[\"']?[ \t]{0,8}[:=][ \t]{0,8}"
                      r"(?i:(?:bearer|basic|token|digest)[ \t]{1,8})?"
                      r"[A-Za-z0-9._~+/=-]{8,}"),
    ("bearer", r"\b(?i:bearer)[ \t]{1,8}[A-Za-z0-9._~+/-]{16,}=*"),
    ("aws-key", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}"),
    ("gcp-key", r"\bAIza[0-9A-Za-z_-]{35}"),
    ("gcp-token", r"\bya29\.[0-9A-Za-z_-]{20,}"),
    ("github-token", r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}"),
    ("github-token", r"\bgithub_pat_[A-Za-z0-9_]{22,}"),
    ("slack-token", r"\bxox[abposr]-[A-Za-z0-9-]{10,}"),
    ("slack-webhook", r"https://hooks\.slack\.com/services/[A-Za-z0-9/_-]{20,}"),
    ("stripe-key", r"\b(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{16,}"),
    ("stripe-key", r"\bwhsec_[A-Za-z0-9]{16,}"),
    ("anthropic-key", r"\bsk-ant-[A-Za-z0-9_-]{20,}"),
    ("openai-key", r"\bsk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_-]{20,}"),
    ("rius-key", r"\b(?:ri|gf)_[A-Za-z0-9]{8,}\.[A-Za-z0-9_-]{8,}"),
    ("jwt", r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*"),
)
_COMBINED = re.compile("|".join("(?P<p%d>%s)" % (i, pattern)
                                for i, (_name, pattern) in enumerate(_PATTERNS)))

# KEY=VALUE and "key": "value" pairs whose key names a secret. Matching
# starts at the secret word, and only a short suffix may follow it, so
# `input_tokens: 12` and `tokenizer=` are left alone. Text may be
# JSON-encoded (tool inputs, a script's JSON.stringify): there `\"`
# quotes a value and `\n` ends a line, so an unquoted value stops at
# either, and an escaped quote only pairs with another escaped quote. That
# keeps the value from swallowing the next line's key, and keeps JSON
# valid after the value goes. The key is kept, so a reader still sees
# which setting it was; only the value goes.
_PAIR = re.compile(
    r"(?i)(?P<key>(?P<word>password|passwd|secret|token|api[_-]?key"
    r"|access[_-]?key|private[_-]?key)(?:[_-]?(?:key|id|value|hash))?"
    r"[\"']?[ \t]{0,8}[:=](?!=)[ \t]{0,8})"
    r"(?P<value>\\\"(?:[^\"\\\n]|\\[^\"\n]){1,512}\\\"|\"[^\"\n]{1,512}\"|'[^'\n]{1,512}'"
    r"|(?:[^\s\"',;&)\\]|\\(?![nrt\"\\])){1,512})")

# Basenames whose contents are secret as a whole: a .env file is nothing
# but values, so no pattern could tell its harmless lines from the rest.
_SECRET_FILES = (".env*", "*.pem", "id_rsa*", "credentials*", "*.key",
                 ".npmrc", ".pypirc", ".netrc")
_PATH_FIELDS = ("file_path", "path", "notebook_path")
_SHELL_SPLIT = re.compile(r"[\s;|&<>()'\"`=]+")

SECRET_FILE_MARKER = "[redacted:secret-file]"


def marker(name: str) -> str:
    return "[redacted:%s]" % name


def _replace_pattern(match) -> str:
    return marker(_PATTERNS[int(match.lastgroup[1:])][0])


def _replace_pair(match) -> str:
    if match.group("value").startswith("[redacted:"):
        return match.group(0)
    word = match.group("word").lower().replace("_", "-")
    return match.group("key") + marker(word)


def scrub(text: str) -> str:
    """`text` with every recognised secret replaced by a marker."""
    if not text:
        return text
    return _PAIR.sub(_replace_pair, _COMBINED.sub(_replace_pattern, text))


def is_secret_path(path: str) -> bool:
    name = re.split(r"[\\/]", path.strip())[-1].lower()
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in _SECRET_FILES)


def reads_secret_file(tool_input_json: str) -> bool:
    """True when a tool call names a secret-shaped file: a path argument
    (Read, Edit, ...) or a word of a shell command (`cat .env`)."""
    try:
        tool_input = json.loads(tool_input_json or "{}")
    except ValueError:
        return False
    if not isinstance(tool_input, dict):
        return False
    for field in _PATH_FIELDS:
        value = tool_input.get(field)
        if isinstance(value, str) and is_secret_path(value):
            return True
    command = tool_input.get("command")
    return isinstance(command, str) and any(
        is_secret_path(word) for word in _SHELL_SPLIT.split(command) if word)


def tool_output(tool_input_json: str, output: Optional[str]) -> str:
    """What may be exported of a tool call's output."""
    if reads_secret_file(tool_input_json):
        return SECRET_FILE_MARKER
    return scrub(output or "")
