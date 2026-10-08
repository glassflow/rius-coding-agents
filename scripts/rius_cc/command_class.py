"""What kind of shell command a tool call ran: test, build, lint, ...

Runs on this machine whatever the capture setting, and only its answer is
sent: one word from CLASSES, never a word of the command. That keeps it
structure, like `Bash.exit_1`. A session page can then say "tests failed"
instead of "Bash failed" without knowing what the command was.

A command is read segment by segment (`cd app && npm test` is two), and
the highest-ranked class among its segments wins: a segment that runs the
tests says more about the call than the `cd` before it. Wrappers such as
`sudo`, `npx`, `uv run` or `timeout 30` are stepped over to the program
they run. Anything not recognised is "other". The reading is plain string
splitting over a bounded prefix, so its cost has a ceiling.
"""
from __future__ import annotations

import json
import re
from typing import Any, Iterable, List, Optional

# Every value the attribute can take. A test asserts nothing else escapes.
CLASSES = ("test", "build", "lint", "package", "git", "other")
_RANK = {name: rank for rank, name in enumerate(reversed(CLASSES))}

ATTRIBUTE = "rius.command.class"
EXIT_CODE_ATTRIBUTE = "process.exit.code"

# Longer commands are read only this far: a heredoc body cannot change
# which program the command starts with.
MAX_CHARS = 4096
_MAX_SEGMENTS = 32

_SEGMENTS = re.compile(r"&&|\|\||[;|&\n]")
_ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_QUOTES = "'\"`()"

# Programs that run the next word as the command, and how many arguments of
# their own come before it.
_WRAPPERS = {"sudo": 0, "time": 0, "env": 0, "nice": 0, "nohup": 0,
             "command": 0, "exec": 0, "caffeinate": 0, "gate": 0, "rtk": 0,
             "npx": 0, "bunx": 0, "pnpx": 0, "uvx": 0, "timeout": 1}
# `uv run pytest`, `pnpm exec eslint`: a runner's subcommand that does the same.
_RUN_SUBCOMMANDS = {"uv": "run", "poetry": "run", "pipenv": "run",
                    "pnpm": "exec", "yarn": "exec", "bundle": "exec",
                    "npm": "exec", "hatch": "run", "pdm": "run"}

_TEST_PROGRAMS = {"pytest", "py.test", "jest", "vitest", "mocha", "ava",
                  "rspec", "phpunit", "tox", "nox", "ctest", "karma",
                  "unittest", "nextest"}
_BUILD_PROGRAMS = {"webpack", "rollup", "esbuild", "cmake", "ninja", "gcc",
                   "g++", "clang", "clang++", "javac", "msbuild", "tsup",
                   "compileall", "build"}
_LINT_PROGRAMS = {"eslint", "ruff", "flake8", "pylint", "mypy", "pyright",
                  "black", "isort", "prettier", "golangci-lint", "gofmt",
                  "shellcheck", "rubocop", "stylelint", "biome", "hadolint",
                  "markdownlint", "actionlint", "tflint", "ktlint",
                  "swiftlint", "dprint", "oxlint"}
_PACKAGE_PROGRAMS = {"brew", "apt", "apt-get", "gem", "composer", "conda",
                     "mamba", "pipx", "pip", "pip3"}
_GIT_PROGRAMS = {"git", "gh"}

_PACKAGE_VERBS = {"install", "i", "ci", "add", "remove", "rm", "uninstall",
                  "update", "upgrade", "up", "sync", "lock", "outdated",
                  "dedupe", "prune", "restore", "get"}
_NODE_RUNNERS = {"npm", "pnpm", "yarn", "bun"}
_TASK_RUNNERS = {"make", "gmake", "just", "task", "mise"}
_JVM_BUILDS = {"gradle", "gradlew", "mvn", "mvnw", "sbt", "bazel", "bazelisk"}

# A script or target name, read by what it is called. Lint words first, so
# `typecheck` is lint and not a test for containing "check".
_SCRIPT_WORDS = (
    ("lint", ("lint", "typecheck", "type-check", "tsc", "fmt", "format",
              "prettier", "eslint", "vet", "style")),
    ("test", ("test", "e2e", "spec", "verify", "check", "coverage")),
    ("build", ("build", "compile", "bundle", "dist")),
)


def classify(command: Any) -> str:
    """The class of a shell command line, "other" when it is not known."""
    if not isinstance(command, str):
        return "other"
    best = "other"
    for segment in _SEGMENTS.split(command[:MAX_CHARS])[:_MAX_SEGMENTS]:
        found = _segment_class(_words(segment))
        if _RANK[found] > _RANK[best]:
            best = found
    return best


def classify_any(commands: Iterable[Any]) -> Optional[str]:
    """The highest class among several commands, None when there are none."""
    found = [classify(c) for c in commands if isinstance(c, str) and c.strip()]
    return max(found, key=_RANK.__getitem__) if found else None


def of_input(tool_input: Any, key: str = "command") -> Optional[str]:
    """The class of a shell tool's input, as a dict or its JSON text: None
    when it carries no command, so a Read or an Edit gets no class."""
    if isinstance(tool_input, str):
        try:
            tool_input = json.loads(tool_input)
        except (ValueError, RecursionError):
            return None
    if not isinstance(tool_input, dict):
        return None
    command = tool_input.get(key)
    if isinstance(command, list):
        command = " ".join(str(word) for word in command)
    if not isinstance(command, str) or not command.strip():
        return None
    return classify(command)


def _words(segment: str) -> List[str]:
    words = [w.strip(_QUOTES) for w in segment.split()]
    words = [w for w in words if w]
    while words:
        head = _program(words[0])
        if _ENV_ASSIGNMENT.match(words[0]):
            words = words[1:]
        elif head in _WRAPPERS:
            words = _drop_options(words[1:])[_WRAPPERS[head]:]
        elif len(words) > 1 and _RUN_SUBCOMMANDS.get(head) == words[1]:
            words = words[2:]
        elif head.startswith("python") and words[1:2] == ["-m"]:
            words = words[2:]
        else:
            break
    return words


def _drop_options(words: List[str]) -> List[str]:
    while words and words[0].startswith("-"):
        words = words[1:]
    return words


def _program(word: str) -> str:
    return word.rsplit("/", 1)[-1].lower()


def _script_class(name: str) -> Optional[str]:
    name = name.lower()
    for found, keywords in _SCRIPT_WORDS:
        if any(word in name for word in keywords):
            return found
    return None


def _segment_class(words: List[str]) -> str:
    if not words:
        return "other"
    program = _program(words[0])
    args = [w.lower() for w in words[1:]]
    first = next((a for a in args if not a.startswith("-")), "")
    if program in _GIT_PROGRAMS:
        return "git"
    if program in _TEST_PROGRAMS:
        return "test"
    if program in _LINT_PROGRAMS:
        return "lint"
    if program == "tsc":
        return "lint" if "--noemit" in args else "build"
    if program in _BUILD_PROGRAMS:
        return "build"
    if program in _PACKAGE_PROGRAMS:
        return "package" if first in _PACKAGE_VERBS or program == "brew" else "other"
    if program in _NODE_RUNNERS:
        return _node_class(program, args, first)
    if program in _TASK_RUNNERS:
        if not first:
            return "build" if program in ("make", "gmake") else "other"
        return _script_class(first) or "other"
    if program in _JVM_BUILDS:
        return "test" if any("test" in a for a in args) else "build"
    return _toolchain_class(program, first)


def _node_class(program: str, args: List[str], first: str) -> str:
    if not first:
        return "package" if program == "yarn" else "other"
    if first in ("run", "run-script"):
        script = next((a for a in args[args.index(first) + 1:]
                       if not a.startswith("-")), "")
        return _script_class(script) or "other"
    if first in ("test", "t"):
        return "test"
    if first in _PACKAGE_VERBS:
        return "package"
    if first in ("exec", "dlx", "x"):
        rest = args[args.index(first) + 1:]
        return _segment_class(rest) if rest else "other"
    # pnpm, yarn and bun run a script named as the subcommand.
    return _script_class(first) or "other"


# Toolchains whose subcommand says what they do: (program, subcommand) -> class.
_SUBCOMMANDS = {
    "go": {"test": "test", "build": "build", "install": "build", "vet": "lint",
           "fmt": "lint", "get": "package", "mod": "package"},
    "cargo": {"test": "test", "nextest": "test", "bench": "test",
              "build": "build", "check": "build", "clippy": "lint",
              "fmt": "lint", "add": "package", "remove": "package",
              "install": "package", "update": "package"},
    "dotnet": {"test": "test", "build": "build", "publish": "build",
               "format": "lint", "add": "package", "restore": "package"},
    "swift": {"test": "test", "build": "build"},
    "deno": {"test": "test", "lint": "lint", "fmt": "lint", "check": "lint",
             "compile": "build", "add": "package", "install": "package"},
    "mix": {"test": "test", "compile": "build", "format": "lint",
            "credo": "lint", "deps.get": "package"},
    "docker": {"build": "build", "buildx": "build"},
    "playwright": {"test": "test"},
    "cypress": {"run": "test"},
    "next": {"build": "build", "lint": "lint"},
    "vite": {"build": "build"},
    "uv": {"add": "package", "remove": "package", "sync": "package",
           "lock": "package", "pip": "package", "build": "build"},
    "poetry": {"add": "package", "remove": "package", "install": "package",
               "update": "package", "lock": "package", "build": "build"},
    "pipenv": {"install": "package", "uninstall": "package", "sync": "package"},
    "bundle": {"install": "package", "update": "package", "add": "package"},
}


def _toolchain_class(program: str, first: str) -> str:
    return _SUBCOMMANDS.get(program, {}).get(first, "other")
