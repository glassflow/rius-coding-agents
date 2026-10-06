"""A code-mode `exec` script, read without being run.

One pass over the text, with no backtracking, finds what the script quotes in
its string literals and which tools it calls (`tools.<name>(...)`), leaving
out comments, regular expressions and the insides of strings. A script it cannot
read to its end (an unterminated string, template or comment, a stray brace)
or that is longer than MAX_CHARS is not trusted to name anything.

Knows nothing about spans (that is codex_spans.py).
"""
from __future__ import annotations

import re
from typing import List, Tuple

MAX_CHARS = 64 * 1024
# What a tool is called, as the model API allows.
_TOOL_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z")
_WORD = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")
_CALL = re.compile(r"\s*\(")
_NUMBER = re.compile(r"[0-9][0-9A-Za-z_.]*")
# After these a `/` starts a regular expression; after anything else it divides.
_REGEX_AFTER_CHARS = frozenset("(,=:[!&|?{};+-*%<>~^")
_REGEX_AFTER_WORDS = frozenset(("return", "typeof", "case", "in", "of", "delete",
                                "void", "throw", "new", "else", "do", "yield",
                                "await"))


class _Reader:
    def __init__(self, text: str) -> None:
        self.text = text
        self.at = 0
        self.literals: List[str] = []
        self.tools: List[str] = []
        self.braces: List[str] = []     # "{" or "${", one per open brace
        self.last = ""                  # the last significant character
        self.last_word = ""

    def read(self) -> bool:
        """Whether the whole text was read."""
        while self.at < len(self.text):
            if not self._step(self.text[self.at]):
                return False
        return not self.braces

    def _step(self, ch: str) -> bool:
        if ch in "\"'":
            return self._quoted(ch)
        if ch == "`":
            self.at += 1
            return self._template()
        if ch == "/":
            return self._slash()
        if ch == "}":
            return self._close_brace()
        if ch.isspace():
            self.at += 1
        elif not self._word_or_number():
            if ch == "{":
                self.braces.append("{")
            self.last, self.last_word = ch, ""
            self.at += 1
        return True

    def _word_or_number(self) -> bool:
        match = _WORD.match(self.text, self.at) or _NUMBER.match(self.text, self.at)
        if match is None:
            return False
        word = match.group()
        if word == "tools" and self.last != ".":
            self._called(match.end())
        self.at = match.end()
        self.last, self.last_word = "a", word
        return True

    def _called(self, after: int) -> None:
        """`tools.<name>(`, `after` the word `tools`."""
        if not self.text.startswith(".", after):
            return
        name = _WORD.match(self.text, after + 1)
        if (name is not None and _TOOL_NAME.match(name.group())
                and _CALL.match(self.text, name.end())):
            self.tools.append(name.group())

    def _quoted(self, quote: str) -> bool:
        text, start = self.text, self.at + 1
        at = start
        while at < len(text):
            ch = text[at]
            if ch == "\\":
                at += 2
            elif ch == quote:
                self.literals.append(text[start:at])
                self.at, self.last, self.last_word = at + 1, "a", ""
                return True
            elif ch == "\n":
                return False
            else:
                at += 1
        return False

    def _template(self) -> bool:
        """The text of a template literal up to its end or its next `${`."""
        text, start = self.text, self.at
        at = start
        while at < len(text):
            ch = text[at]
            if ch == "\\":
                at += 2
            elif ch == "`":
                self.literals.append(text[start:at])
                self.at, self.last, self.last_word = at + 1, "a", ""
                return True
            elif ch == "$" and text.startswith("{", at + 1):
                self.literals.append(text[start:at])
                self.braces.append("${")
                self.at, self.last, self.last_word = at + 2, "{", ""
                return True
            else:
                at += 1
        return False

    def _close_brace(self) -> bool:
        if not self.braces:
            return False
        self.at += 1
        self.last, self.last_word = "}", ""
        return self._template() if self.braces.pop() == "${" else True

    def _slash(self) -> bool:
        text, at = self.text, self.at
        following = text[at + 1:at + 2]
        if following == "/":
            end = text.find("\n", at)
            self.at = len(text) if end < 0 else end
            return True
        if following == "*":
            end = text.find("*/", at + 2)
            self.at = at + 2 if end < 0 else end + 2
            return end >= 0
        if self._starts_regex():
            return self._regex()
        self.at, self.last, self.last_word = at + 1, "/", ""
        return True

    def _starts_regex(self) -> bool:
        if self.last == "a":
            return self.last_word in _REGEX_AFTER_WORDS
        return self.last == "" or self.last in _REGEX_AFTER_CHARS

    def _regex(self) -> bool:
        text, at, in_class = self.text, self.at + 1, False
        while at < len(text):
            ch = text[at]
            if ch == "\\":
                at += 2
                continue
            if ch == "\n":
                return False
            if ch == "/" and not in_class:
                self.at, self.last, self.last_word = at + 1, "a", ""
                return True
            if ch in "[]":
                in_class = ch == "["
            at += 1
        return False


def read(script: str) -> Tuple[List[str], List[str], bool]:
    """(the string literals, the tools called in order, whether the script
    was read to its end). A script too long to read gives nothing."""
    if len(script) > MAX_CHARS:
        return [], [], False
    reader = _Reader(script)
    complete = reader.read()
    return reader.literals, reader.tools, complete


def called_tools(script: str) -> List[str]:
    """The tools a script calls, in order, each once: none for a script that
    could not be read to its end, whose names could be anything."""
    _, tools, complete = read(script or "")
    return list(dict.fromkeys(tools)) if complete else []


def string_literals(script: str) -> List[str]:
    """What the script quotes, as far as it could be read."""
    return read(script or "")[0]
