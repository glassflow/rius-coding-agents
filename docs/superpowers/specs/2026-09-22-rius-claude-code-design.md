# rius-coding-agents — design

**Date:** 2026-09-22
**Status:** approved, not yet implemented
**Repo:** `rius-coding-agents` · **This deliverable:** the `rius-claude-code` plugin

A Claude Code plugin that streams the current Claude Code session to Rius as OTLP
GenAI traces, live, opt-in per folder.

The repo is named for the category because a second coding agent is expected
eventually; the code in it is Claude Code only, and deliberately so (§14).

## 1. Purpose and scope

Claude Code is a long-running, tool-using, autonomous LLM agent — the exact
workload Rius exists to observe — and it is currently invisible to it. This
plugin makes a Claude Code session show up in Rius the way an instrumented
`glassflow-rius` application does: a trace per session, generations with token
usage and cost, tool calls with inputs and outputs, subagents nested under the
tool call that spawned them.

It ships as a public integration, not an internal tool. It must work for someone
who has never seen this repo, on a machine with nothing but Claude Code and a
system `python3`.

### In scope

- A Claude Code plugin installable from a marketplace.
- Live export: spans appear in Rius while the session is still running.
- Per-folder opt-in, with a per-session override in both directions.
- Full content capture (prompts, assistant text, tool inputs and outputs) by
  default, with an opt-out.

### Out of scope

These are deliberate exclusions, not oversights:

- **Backfilling history.** ~1,091 existing transcripts (528 MB) sit in
  `~/.claude/projects/`. Importing them is a separate tool with a separate set of
  problems (rate limiting the ingest, backdated timestamps, retention windows).
  Not this project.
- **Claude Code's native OTEL path** (`CLAUDE_CODE_ENABLE_TELEMETRY=1`). It emits
  metrics and log events, not spans; Rius ingests `/v1/traces`. Using it would
  mean backend work in `argus-core`.
- **Any change to `argus-core`.** This is a pure client. If it turns out to need
  a backend change, that is a separate ticket, raised rather than worked around.
- **Support for any other coding agent**, and any abstraction anticipating one.
  See §14 for why, and for what the second adapter would cost.

## 2. Source of truth

The session transcript, `~/.claude/projects/<slug>/<sessionId>.jsonl`, plus the
per-subagent transcripts beside it in `<sessionId>/subagents/` (§3.3), are the
only data sources. Hook payloads are used for *timing and control* — which hook
fired, for which session, with which `transcript_path` — never as a second,
divergent copy of the data.

Verified fields on real transcript entries:

| Entry | Fields used |
|---|---|
| `assistant` | `uuid`, `parentUuid`, `timestamp`, `requestId`, `message.model`, `message.usage`, `message.stop_reason`, `message.content[]`, `cwd`, `gitBranch`, `version`, `isSidechain` |
| `user` | `uuid`, `parentUuid`, `timestamp`, `promptId`, `message.content[]`, `sourceToolAssistantUUID`, `toolUseResult` |
| tool result block | `tool_use_id`, `is_error`, `content` |

`message.usage` carries `input_tokens`, `output_tokens`,
`cache_creation_input_tokens`, `cache_read_input_tokens`, and
`output_tokens_details.thinking_tokens`.

Non-conversational line types (`last-prompt`, `mode`, `permission-mode`,
`ai-title`, `cost-state`, `file-history-snapshot`, `attachment`) are skipped.
Unknown line types are skipped silently — Claude Code adds them without notice,
and an unrecognised line must never be an error.

## 3. Span model

One trace per session.

```
AGENT   claude-code session          parent=''
└─ CHAIN  turn N
   ├─ LLM   assistant message
   │  ├─ TOOL  Read / Bash / mcp__…
   │  └─ TOOL  Agent            (the call that spawned a subagent)
   │     └─ AGENT  subagent     (its own transcript file -- see §3.3)
   │        └─ LLM   the subagent's generation
   │           └─ TOOL  a tool the subagent called
```

- **Root (`AGENT`)** — the session. `ParentSpanId` is empty, which is what makes
  it a root to the backend (`packages/clickhouse-db/spans/reader.go:82`:
  `rootSpanPredicate = "(ParentSpanId = '0000000000000000' OR ParentSpanId = '')"`).
  A trace whose root never arrives will not appear in the traces list, so the
  root is emitted as a pending span at `SessionStart` (see §5).
- **Turn (`CHAIN`)** — one user prompt and everything it caused. Grouped by
  `promptId` on user entries.
- **Generation (`LLM`)** — one assistant message.
- **Tool (`TOOL`)** — one `tool_use` block, child of the generation that
  requested it, ended by the matching `tool_result`.
- **Subagent (`AGENT`)** — one subagent run, nested under the `Agent`/`Task`
  tool span that spawned it, with that subagent's own generations and tool
  calls beneath it. Its entries live in a **separate transcript file**, not
  inline in the session transcript; see §3.3.

Relationships come from real fields, never from positional inference:
`tool_use.id` ↔ `tool_result.tool_use_id`, `sourceToolAssistantUUID`,
`promptId`, `parentUuid`.

### 3.1 Identifiers

All IDs are **derived, never stored**:

```
trace_id = sha256("rius-cc:trace:" + sessionId).digest()[:16]   # 32 hex
span_id  = sha256("rius-cc:span:"  + entry.uuid ).digest()[:8]  # 16 hex
```

Synthetic spans that have no transcript entry of their own use a namespaced key
instead of a `uuid`:

```
root span_id = sha256("rius-cc:span:session:" + sessionId).digest()[:8]
turn span_id = sha256("rius-cc:span:turn:"    + promptId ).digest()[:8]
```

This is the property the whole architecture rests on: **any process, at any
time, re-derives the same IDs from the transcript alone.** No ID is ever handed
between processes, so a per-event exporter is stateless with respect to
identity, and a replay is idempotent rather than duplicative.

The 8-byte truncation of SHA-256 gives a collision probability below 1e-9 for
any plausible session size (birthday bound over ~10^5 spans), and collisions are
scoped to a single trace.

### 3.2 Attributes

The vocabulary is `glassflow-rius`'s own (`src/rius/semconv.py`), not invented
here, and not the upstream OTel GenAI spelling where the two differ.

| Attribute | Value |
|---|---|
| `openinference.span.kind` | `AGENT` / `CHAIN` / `LLM` / `TOOL` |
| `gen_ai.operation.name` | `invoke_agent` / `chat` / `execute_tool` |
| `gen_ai.provider.name` | `anthropic` |
| `session.id` | Claude Code `sessionId` |
| `gen_ai.request.model`, `gen_ai.response.model` | `message.model` |
| `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens` | from `message.usage` |
| `gen_ai.usage.cache_read.input_tokens` | `cache_read_input_tokens` |
| `gen_ai.usage.cache_creation.input_tokens` | `cache_creation_input_tokens` |
| `gen_ai.response.finish_reasons` | `[message.stop_reason]` |
| `gen_ai.tool.name` | tool name |
| `gen_ai.agent.name` | subagent `agentType` — the key argus-core's sink filters on (`docs/spans-query.md`) |
| `gen_ai.agent.description` | subagent `description` (content, see §7) |
| `cc.subagent.id` / `cc.subagent.depth` | which subagent file, and how deep it sits |
| `cc.turn.source` | `user` or `system` for a turn (§3.4) |
| `input.value` / `output.value` | content (see §7) |

Note the cache-token keys are **Rius-specific spellings**
(`gen_ai.usage.cache_read.input_tokens`), not the upstream GenAI convention.

Resource attributes: `service.name` (default `claude-code`, overridable),
`service.instance.id` (a UUID per session), plus `cc.version`, `cc.cwd`,
`cc.git_branch` for the session.

**The instrumentation scope name must be `glassflow`.** `semconv.py` states the
value is wire-visible and the backend keys on it; it stayed `glassflow` through
the Rius rebrand on purpose. Emitting anything else risks spans being ignored
downstream. A test asserts this literal.

### 3.3 Subagents live in their own transcript files

**As-built, verified against a real session — an earlier draft of this spec
said subagent entries appear inline in the session transcript as
`isSidechain: true` lines. They do not.** The main transcript contains zero
sidechain entries. Every subagent writes its own file:

```
~/.claude/projects/<slug>/<sessionId>/subagents/agent-<agentId>.jsonl
~/.claude/projects/<slug>/<sessionId>/subagents/agent-<agentId>.meta.json
```

The `.jsonl` is the same format as the session transcript (its entries carry
`isSidechain: true`) and is read by the same parser. The `.meta.json` carries:

| Field | Use |
|---|---|
| `toolUseId` | the exact `tool_use` id of the `Agent` call that spawned this subagent |
| `agentType` | `gen_ai.agent.name` on the subagent's `AGENT` span |
| `model` | `gen_ai.request.model` on that span |
| `description` | `gen_ai.agent.description` (content; see §7) |
| `spawnDepth` | how deep the subagent sits; subagents can spawn subagents |

Why this matters: in the acceptance session, **58% of all tokens and 71% of
all generations happened inside subagents** (main 799,702 tokens / 16
generations; subagents 1,101,800 / 40). A trace that stops at the tool span
reports less than half of what the session actually cost.

Rules:

1. **Exact linkage or none.** The spawning tool span is matched to a subagent
   file by `toolUseId` ↔ `tool_use.id`, and by nothing else. No mtime, no
   ordering, no "the only one that could be". If no `.meta.json` names a given
   `tool_use`, the `TOOL` span is emitted alone and that is the end of it —
   guessing files one agent's tokens under another agent's name, which is
   worse than the gap it papers over.
2. **Own offset, same streaming.** Each subagent file has its own byte offset,
   `state["sub_offsets"][agentId]`, so its spans stream out per hook event
   exactly like the main transcript's. Per-agent span bookkeeping lives in
   `state["sub_scopes"]`, and the tool_use → tool span map in
   `state["sub_links"]`. All of it is plain JSON; the whole state dict is
   written to disk between hook invocations.
3. **The subagent's `AGENT` span** starts pending when the `Agent` tool_use is
   seen and closes when the matching `tool_result` arrives in the *main*
   transcript — the subagent's own file has no closing entry. A session that
   ends mid-subagent closes it at `SessionEnd`, like the root.
4. **Recursion.** A subagent's own `Agent` tool_use registers another link
   while its entries are being read, so nesting works to arbitrary depth,
   capped by `subagents.MAX_DEPTH` (5) so that a cycle cannot turn one hook
   event into an unbounded walk of the filesystem.
5. **Same trace, derived ids.** A subagent's spans carry the parent session's
   trace id — one trace per session — and its `AGENT` span id is
   `sha256("subagent:" + agentId)`, so replay stays idempotent.
6. **Turn spans are not created inside a subagent scope**; its generations
   hang directly off its `AGENT` span. (A subagent transcript repeats the
   *parent's* `promptId`, so any turn span built inside one must namespace its
   key by agent id or it will collide with the main session's turn span.)
7. The tool name is **`Agent`** in Claude Code 2.1.x and was `Task` before
   that. Both are matched, via `spans.SUBAGENT_TOOL_NAMES`.

### 3.4 Turn provenance

Not every `user` entry is something the user typed. The harness injects turns
whose text opens with `<local-command-caveat>`, `<task-notification>`,
`<agent-message …>`, `<bash-input>` and friends. They are real work, so they
keep their turn span rather than being dropped, and carry
`cc.turn.source = "system"`; a genuine prompt carries `"user"`. The match is
anchored at the start of the text, never a substring search — a prompt that
merely mentions `<bash-input>` is still a prompt.

## 4. Timing

Transcript timestamps are **completion** times — the moment the entry was
written. That gives two different qualities of span:

- **Tool spans are accurate.** Start is the timestamp of the assistant entry
  carrying the `tool_use`; end is the timestamp of the `tool_result` entry. Both
  are real event times.
- **Generation spans are approximate.** Nothing in the transcript records when
  the request was dispatched, so start is taken as the timestamp of the
  preceding entry in the `parentUuid` chain. A generation's duration therefore
  absorbs any gap before the call — user think time, permission prompts, tool
  scheduling.

This is documented in the README rather than hidden. Narrowing it would require
a request-start signal that does not currently exist; inventing one client-side
would make the number look precise while staying wrong.

Span start/end are emitted as Unix nanoseconds, parsed from the RFC3339
timestamps (which are UTC, `Z`-suffixed).

## 5. Liveness

Rius has a first-class representation for a span that has started but not
finished: `glassflow.span.pending` (`semconv.py:71`). A content-free snapshot is
exported at span start, the backend maps it to `Finished=0`, and the real span
replaces it at end. `PENDING_IDENTITY_ATTRIBUTES` is the allowlist of attributes
permitted to ride a snapshot — and `session.id` is on it explicitly "because a
pending span must be groupable into its session while still running, that is the
live view's whole point."

So liveness uses the platform's own mechanism. Hook events map onto span
boundaries:

| Hook | Emits |
|---|---|
| `SessionStart` | pending root |
| `UserPromptSubmit` | pending turn |
| `PreToolUse` | pending tool |
| `PostToolUse` | final tool, and the final `LLM` span for the message that requested it |
| `Stop` | final turn |
| `SessionEnd` | final root |

A pending span carries only attributes from `PENDING_IDENTITY_ATTRIBUTES` and
the `gen_ai.request.*` prefix. No content is ever on a snapshot, which also
means the content cap and capture setting in §7 apply only to final spans.

A session killed mid-flight leaves pending spans unresolved. That is the correct
outcome — it is what "this agent died while running" looks like — not a bug to
paper over with a synthetic end.

## 6. Runtime

### 6.1 Two processes

`hooks.json` wires six events to `hook.py <event>`.

**`hook.py`** — runs in Claude Code's critical path, so it does as little as
possible: read the hook JSON from stdin, resolve config (§8, no network, no
transcript parse), and if tracing is disabled for this session, exit 0. If
enabled, spawn `exporter.py` **detached** (`setsid`, stdio to the log file, no
wait) and exit 0. Target: under ~30 ms.

**`exporter.py`** — runs off the critical path. Per session it:

1. Takes an `flock` on `~/.claude/rius/state/<sessionId>.lock`, non-blocking. If
   another exporter holds it, exit — that process will pick up the new lines,
   since it reads to EOF.
2. Reads `~/.claude/rius/state/<sessionId>.json`: `{offset, open_spans}`.
3. Reads the transcript from `offset` to EOF, parsing complete lines only. A
   trailing partial line is not consumed and `offset` stops before it.
4. Assembles spans, resolving `open_spans` (a `tool_use` seen in an earlier
   invocation whose `tool_result` has only now arrived).
5. POSTs one OTLP/HTTP JSON payload to `<endpoint>/v1/traces`.
6. Writes state back, then releases the lock.

### 6.2 Dependencies

Stdlib only: `json`, `hashlib`, `struct`, `urllib.request`, `fcntl`, `os`,
`subprocess`. No `glassflow-rius`, no OpenTelemetry SDK, no `protobuf`.

This is forced by the packaging choice — a Claude Code plugin cannot bring its
own virtualenv, so it may only use a runtime the user already has. It also keeps
hook latency off the OTel SDK's import time. The cost is real and acknowledged:
a slice of the SDK's wire format and its semconv is reimplemented here, and it
can drift. §10 covers how that is defended.

### 6.3 Wire format: protobuf, hand-encoded

**Resolved 2026-09-22.** The receiver is protobuf-only. `apps/receiver/internal/handler/handler.go:182`
rejects any `Content-Type` that is not `application/x-protobuf` with 415:
*"OTLP/JSON is deferred; anything non-protobuf is unsupported."*

So the exporter encodes OTLP protobuf itself, in stdlib Python. This is
tractable because it is **encode-only**: no parsing, no unknown fields, no
schema evolution. The subset needed is `TracesData` → `ResourceSpans` →
`ScopeSpans` → `Span`, plus `KeyValue`/`AnyValue`, `Status` and `Event` — a
fixed, public, stable schema requiring only varint, length-delimited and
fixed64 encoding.

Correctness is not assumed. CI encodes every fixture with our encoder and
decodes it with the real `opentelemetry-proto` package — a **test-only**
dependency, never shipped — asserting field-for-field equality. The plugin
keeps zero runtime dependencies; the real library supplies the guarantee.

The alternative, switching to a pip package that depends on the OTel SDK, was
rejected because it reverses the packaging decision and its install story for a
problem worth ~200 lines.

**Separately worth raising:** OTLP/JSON support is required by the OTLP
specification, so the receiver has a compliance gap that will affect other
integrations. That is an `argus-core` ticket, not this project's work.

## 7. Content

Full content — user prompts, assistant text, tool inputs, tool outputs — is
captured **by default**.

This is a deliberate product decision with a real cost, and the README must lead
with it rather than bury it: with this default, installing the plugin sends the
contents of files Claude Code read, the output of commands it ran, and anything
those happened to contain, off the machine. That includes secrets in a `.env` a
session happened to open. The default of **off per folder** (§8) is what keeps
this bounded — nothing is sent until a folder is deliberately opted in.

Controls:

- `RIUS_CAPTURE_CONTENT=false` — strips all content attributes, keeping
  structure, models, tokens, cost and status. Mirrors the SDK's env var of the
  same name, so the two behave identically.
- `RIUS_CLAUDE_MAX_ATTR_BYTES` (default 32768) — per-value cap. Oversized values
  are truncated with an explicit ` …[truncated N bytes]` marker, so a truncated
  value is never mistaken for a complete one. This exists for transport, not
  privacy: a single multi-megabyte file read would otherwise break the export.

Content attribute keys are exactly the SDK's `CONTENT_ATTRIBUTES` set, so
"content" means the same thing in both codebases.

A subagent's brief, its `description` and its tools' output are content and go
through the same gate; with capture off, a failed tool's `Status.message`
reads `tool error (detail withheld: RIUS_CAPTURE_CONTENT=false)` rather than
carrying the failure's output — the point being that a reader can tell a
deliberate omission from a missing value.

**With capture off, nothing content-bearing is written to the state file
either** — not the turn text, not a cached subagent brief, and not an open
tool's input (for an `Agent` call that input *is* the subagent's brief). This
matters because `state.save` writes that dict to
`~/.claude/rius/state/<sessionId>.json` in plaintext on every hook event, and
an open tool or a running subagent sits in it for the whole of its run; the
values are only ever read back to fill `input.value`, which the gate drops
anyway. Three defects of this exact shape have shipped, so the rule is
enforced generically rather than per site: `tests/test_content_never_in_state.py`
extracts every content string from every fixture transcript, replays it with
`RIUS_CAPTURE_CONTENT=false`, and asserts none of them appears anywhere in the
state — after each entry, mid-flight, not only at completion. A fixture added
later is covered the day it lands.

## 8. Configuration and scoping

### 8.1 Resolution

Evaluated on every hook invocation, first match wins:

1. **Per-session toggle** — `~/.claude/rius/sessions/<sessionId>`, written by
   `/rius on` / `/rius off`. Overrides everything in both directions, including
   turning tracing *on* for one session in a folder that is otherwise excluded.
2. **`.claude/settings.local.json`** — personal, gitignored.
3. **Project `.claude/settings.json`** — checked in, so a repo can ship "never
   trace this one" to everyone who clones it.
4. **Path rules** — `~/.claude/rius/config.json`, globs matched against `cwd`.
5. **Global default: off.**

Layers 2 and 3 are read as an `env` entry (`RIUS_CLAUDE_ENABLED`). Claude Code
already merges its settings hierarchy and exposes `env` to hook processes, so
the plugin reads the resolved value rather than reimplementing the merge. This
is why the hierarchy is reused rather than replaced: a parallel config system
would be one more thing to explain and one more thing to disagree with the
first.

### 8.2 Default off

Nothing is traced until a folder is explicitly enabled. Combined with §7, this
means installing the plugin cannot silently upload a repo nobody thought about.

The cost is that a correctly-installed, not-yet-enabled plugin and a broken one
look identical. `/rius status` therefore must state the resolved decision **and
the layer that produced it** — "off: no path rule matches `/x/y`, global default
is off" — not merely "off".

### 8.3 Settings

| Variable | Default | Meaning |
|---|---|---|
| `RIUS_API_KEY` | — | required; absent means disabled |
| `RIUS_ENDPOINT` | `https://ingest.eu.console.rius-glassflow.com` | traces go to `<endpoint>/v1/traces` |
| `RIUS_SERVICE_NAME` | `claude-code` | `service.name` |
| `RIUS_CLAUDE_ENABLED` | unset | per-folder override (§8.1 layers 2–3) |
| `RIUS_CAPTURE_CONTENT` | `true` | §7 |
| `RIUS_CLAUDE_MAX_ATTR_BYTES` | `32768` | §7 |
| `RIUS_CLAUDE_DEBUG` | `false` | verbose log to `~/.claude/rius/log/` |

Defaults match the SDK's where a name is shared.

### 8.4 Commands

- `/rius on --session <id>` / `/rius off --session <id>` — that session only.
  `--session` is mandatory: these write a per-session override and must never
  guess which session they are acting on.
- `/rius clear --session <id>` — remove that per-session override, falling
  back to the normal resolution ladder.
- `/rius enable-here` — add `cwd` to the path rules.
- `/rius status` — resolved state, the layer that decided it, endpoint,
  workspace, spans exported this session, and the last export error if any.
  This is the one action allowed to infer a session id, because it only
  reads; when it does, it says so.

## 9. Failure handling

**A hook always exits 0.** Every entry point wraps its body; any exception is
logged and swallowed. Observability that can break the session it observes is
worse than no observability.

| Failure | Behaviour |
|---|---|
| No API key | Disabled. `/rius status` says which setting is missing. |
| Export HTTP failure | One retry with a short backoff, then drop the batch and log. Never block, never retry indefinitely. |
| Transcript compacted or rewritten (`offset` > file size) | Reset `offset` to 0 and re-derive. Replay is idempotent by §3.1, and the backend's `ReplacingMergeTree` collapses byte-identical rows. |
| Corrupt or unparseable line | Skip the line, advance the offset, log once. |
| Concurrent exporters | `flock` per session; the loser exits rather than queues. |
| Concurrent sessions | Fully independent — state, lock and trace are all keyed by `sessionId`. |
| State file corrupt | Discard it, reset to `offset: 0`. |

Dropping spans is always preferred over delaying or breaking the session.

## 10. Testing

- **Fixture transcripts.** Sanitized transcripts checked into
  `tests/fixtures/`, covering: a plain text turn, a tool call, a failed tool call
  (`is_error`), an inline sidechain (older Claude Code versions), a compacted
  session, a truncated trailing line, and harness-injected turns
  (`system_turns.jsonl`, §3.4).
- **Subagent fixture set.** `tests/fixtures/subagent_files/` is a whole session
  directory: a main transcript with an `Agent` tool_use, plus
  `<sessionId>/subagents/agent-*.jsonl` and their `.meta.json`, including a
  depth-2 subagent spawned by a subagent. `tests/fixtures/subagent_orphan/` is
  the same shape with no `.meta.json` naming the tool_use, for the
  tool-span-alone fallback. Covered in `tests/test_subagents.py`: nesting under
  the right `TOOL` span, subagent token usage reaching the trace, incremental
  streaming via per-agent offsets, the depth cap, and the capture-off gate.
- **Golden payloads.** Fixture → expected OTLP JSON, asserted whole. This is the
  main defence against semconv drift, because a change in any attribute key
  fails visibly.
- **Unit tests.** ID derivation stability (a literal expected hash, so the
  derivation cannot change silently and orphan in-flight sessions); config
  precedence across all five layers; span-tree assembly; truncation marker;
  pending-span attribute allowlist.
- **Scope name test.** Asserts the literal `glassflow` (§3.2).
- **End to end.** A stdlib `http.server` fake OTLP receiver; assert the request
  path, the `Authorization` header and the payload.
- **Manual acceptance.** A real session against Rius staging
  (`https://ingest.staging.rius.glassflow.xyz`), verifying in the UI that the
  trace appears *while the session is still running*, the waterfall nests
  correctly, and token counts match the transcript.

## 11. Layout

```
.claude-plugin/plugin.json
hooks/hooks.json
scripts/hook.py              # critical path: resolve config, spawn, exit
scripts/rius_cc/
    config.py                # §8 resolution
    transcript.py            # JSONL → entries
    spans.py                 # entries → span tree, §3
    subagents.py             # subagent transcript files → nested spans, §3.3
    proto.py                 # protobuf wire primitives + OTLP encoding, §6.3
    otlp.py                  # span tree → OTLP payload, export, §6
    state.py                 # offset + open_spans, flock
commands/rius.md
tests/
docs/superpowers/specs/
README.md
```

Modules split on the seams that let each be tested alone: `transcript.py` knows
nothing of spans, `spans.py` nothing of HTTP, `otlp.py` nothing of Claude Code.

## 12. Decisions and their alternatives

| Decision | Rejected alternative | Why |
|---|---|---|
| Transcript as source of truth | Hook payloads as data | Hooks carry no token usage or model; the transcript carries everything. |
| Derived IDs | Stored ID map | Removes all cross-process identity state; makes replay idempotent. |
| Stateless per-event exporter | Per-session tailer daemon; one global daemon | No background lifetime to get wrong. Degrades to "slightly stale", not "silently dead". Orphaned daemons on a user's machine are an uninstall-grade bug. |
| Pending spans | Re-emitting the root with a growing duration | The platform already has the mechanism; the workaround would have been invisible to the UI's `Finished` handling. |
| One trace per session | Trace per turn | Matches "one long-running agent run", which is Rius's framing. Accepted cost: heavy sessions make large traces. |
| Plugin, stdlib-only | pip package; Go binary | Native install, no runtime the user lacks. Accepted cost: reimplements a slice of the SDK. |
| Default off per folder | Default on | With full-content capture, default-on means installing starts uploading every repo touched. |
| Claude Code only, no adapter interface | A generic coding-agent abstraction now | One known adapter is not enough to shape an interface. §14. |

## 13. Open questions

1. ~~Does the receiver accept OTLP/HTTP JSON?~~ **Resolved:** protobuf only.
   See §6.3.
2. **Does the Rius UI render an unresolved pending root usefully** for a session
   running for hours, or does it need an end to look right? Resolve during
   manual acceptance (§10).
3. **Cost attribution.** The sink resolves `Cost` from token counts and model.
   Confirm `claude-opus-5` and the cache-token split are priced in
   `packages/argus-core/pricing`, or costs read as zero.

## 14. Other coding agents

The repo is named for the category. The code is Claude Code only, and no
adapter interface is written until a second agent exists.

### 14.1 What is already agent-agnostic

| Module | Agent-specific? |
|---|---|
| `otlp.py` | No — span tree to OTLP protobuf, and export |
| `state.py` | No — offset, open spans, flock |
| `spans.py` | No in concept — session → turn → generation → tool is every coding agent's shape; the Rius semconv mapping is shared |
| `config.py` | Partly — the resolution ladder is generic, layers 2–3 read Claude Code's settings files |
| `transcript.py` | **Yes** — the JSONL schema |
| `hook.py`, `hooks.json`, `commands/`, plugin manifest | **Yes** |

The seams in §11 already fall in the right place: `transcript.py` knows nothing
of spans, `spans.py` nothing of HTTP, `otlp.py` nothing of Claude Code.

### 14.2 Why no abstraction now

Coding agents differ in exactly the two places hardest to abstract: how you
observe a running session, and what its record of that session looks like.

Claude Code hands us a hook system firing at precisely the span boundaries we
need — that is unusually convenient, and §5's liveness depends on it entirely.
An agent with no hook system cannot use this architecture at all; it needs a
file watcher or an API proxy, which is a different §6, not a different
`transcript.py`. An interface written today would be shaped by Claude Code's
conveniences and would be wrong for the first agent that lacks them.

So the second adapter is what defines the interface, not the first. Writing it
now would be guessing.

### 14.3 What the second adapter costs

`transcript.py` and the hook layer move to `adapters/claude_code/`; the new
agent gets a sibling directory; `spans.py`, `otlp.py` and `state.py` do not
move. That is a refactor of two files against two real implementations —
cheaper, and better informed, than an interface designed against one.

If the second agent has no hook system, §6 is re-opened for that adapter alone.
The export path below `spans.py` is unaffected either way, which is the part
worth protecting.
