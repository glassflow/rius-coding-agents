# Live Cursor harness

Runs the real `cursor-agent` against this checkout of the plugin and checks the
traces it sends. It is not part of `pytest` (nothing here is named `test_*`):
a run uses your Cursor plan and takes minutes.

    python tests/live/cursor_live.py list        # the scenario names
    python tests/live/cursor_live.py all         # every scenario, one at a time
    python tests/live/cursor_live.py headless_shell
    python tests/live/cursor_live.py serve       # a receiver and two folders, for a hand test
    python tests/live/cursor_live.py cleanup     # undo a run that crashed

It needs a **paid Cursor plan** and `cursor-agent` logged in (`cursor-agent
status`). A quota or plan error from Cursor fails the scenario with Cursor's
own message; it is not worked around. `CURSOR_LIVE_MODEL` picks the model
(default: the first of `gemini-3.7-flash-high`, `composer-2.5`, `auto` your
account lists). `CURSOR_LIVE_REQUIRE_TOKENS=1` makes `tokens` require
`gen_ai.usage.input_tokens` and `output_tokens` above 0 on the LLM span.

## Scenarios

| name | checks |
|---|---|
| `headless_shell` | `cursor-agent -p` runs `true` then `false`: AGENT root `cursor session`, a CHAIN turn, an LLM span, two Shell TOOL spans (one ERROR), `service.name=cursor`, no content attributes |
| `headless_subagent` | the Task tool: an AGENT span under the Task TOOL span |
| `headless_resume` | `--resume` of that chat: the same trace, a second turn |
| `content` | in the content folder, a one-tool run: `input.value` and `output.value` on the TOOL span |
| `interactive_tools` | a pty-driven `cursor-agent` runs a shell command and a Task: same shape, and prompt and stop events in the spool |
| `interactive_status` | `/rius-status` prints `Rius tracing: on` |
| `tokens` | one finished interactive turn; lists every key of the raw `stop` payload |

Each run writes `tests/live/runs/<UTC time>/` (git-ignored): `summary.md`,
`spans.jsonl` (what the receiver got), the copied `spool/` and `log/`, and
`home-before.txt` / `home-after.txt` (`ls -la ~/.cursor`).

## What it touches

Before it starts it aborts if `scripts/rius_cc/_tests_trust_env_home.py`
exists, if `~/.cursor/rius` exists, if `~/.cursor/plugins/local/rius`
exists, or if Cursor is not logged in. Then, all of it recorded in
`~/.cursor/rius/.rius-live-harness` and undone at the end (also on Ctrl-C and
SIGTERM):

- `~/.cursor/plugins/local/rius`: a symlink to this checkout (and
  `plugins/` and `plugins/local/` if they were missing);
- `~/.cursor/rius`: a dummy key (`ri_xxxx...`, environment `local`) and the
  state, spool and logs of the run;
- two folders under the system temp dir, one tracing structure only
  (`enable-here`), one tracing content (`content-on-here`);
- `~/.cursor/hooks.json`, only if the plugin did not load through the symlink
  (it then runs `install-hooks`) or for `tokens` (a hook that records the key
  names and token numbers of the `stop` payload, no text). The harness never
  changes a `hooks.json` that was already there; it stops instead.

A local receiver listens on `127.0.0.1:4318`, the `local` environment's
ingest port. If something else holds that address it aborts; a listener on
the wildcard address (`0.0.0.0`) may go unnoticed on macOS and Linux.
`serve` skips the `cursor-agent status` login check, since it runs no model.

It does not touch Cursor's own state: `cursor-agent` writes its transcripts
and chats under `~/.cursor/projects/<folder slug>/` for each folder it runs
in, and those stay (the folders are in the system temp dir and are removed).
`home-before.txt` and `home-after.txt` therefore differ by those entries.

`cursor-agent --plugin-dir <path>` also loads a plugin from a folder; this
harness uses the symlink, which is how an installed local plugin is found.
