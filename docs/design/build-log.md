# Build log — rius-claude-code

The working record of how this plugin was built: every ruling made, every defect found,
and how each was caught. Preserved from the build ledger because the failure modes here
are more instructive than the final code.

Branch: feat/rius-claude-code (in-place, not a worktree)
BASE: f5a5770d3c5789d80312ce3823912dea31978dc5
Spec: docs/design/specs/2026-09-22-rius-claude-code-design.md (reachable)

Ruling: work in-place on branch feat/rius-claude-code rather than a git worktree — repo is
brand new with no competing work, and the plugin dev loop installs from this exact path, which a
worktree would move. Cost if wrong: none beyond losing worktree isolation we have no use for.

## Pre-flight conflict scan

| Pair | Produces -> Consumes | Finding |
|---|---|---|
| 0 -> all | rius_cc package + pytest pythonpath | clean |
| 1 -> 6 | proto primitives -> otlp.encode | clean, all 8 names match |
| 2 -> 3 | Entry attrs/methods -> spans.build | clean; every attr T3 uses is in T2's Produces |
| 3 -> 6,10 | Span kwargs -> otlp.encode | clean; identical 11 kwargs in T3, T6, T10 |
| 3 -> 5,7 | state dict keys | clean; T5 new_state() lists the same 7 keys T3 defines |
| 4 -> 7,9 | Config fields | clean |
| 5 -> 7 | load/save/session_lock | **CONFLICT** — see ruling 1 |
| 6 -> 7,10 | encode/export | clean |
| 7 -> 8,10 | exporter.run/main <- hook payload file | **CONFLICT** — see ruling 2 |
| 9 -> 4 | rius_ctl -> config paths via HOME | clean |
| 10 -> 0 | fixtures_dir fixture | clean |

| Task | Self-consistency (tests vs code it specifies) | Finding |
|---|---|---|
| 0 | scaffold vs pytest invocation | clean |
| 1 | 9 tests vs complete code given | clean |
| 2 | 8 tests vs complete code given | clean |
| 3 | 11 tests vs prose spec | **CONFLICT** — see ruling 3 |
| 4 | 11 tests vs prose spec | clean |
| 5 | 8 tests vs prose spec | covered by ruling 1 |
| 6 | 9 tests vs prose spec + field table | clean |
| 7 | 8 tests vs prose spec | covered by ruling 2 |
| 8 | 6 tests vs complete code given | clean |
| 9 | 6 tests vs prose spec | clean |
| 10 | 2 tests vs complete code given | clean |
| 11 | manual acceptance only | clean (no automated gate by design) |

Ruling 1: Task 5's Produces block omits `state_path`, but its own tests call
`state.state_path(session_id, home)`. Adding `state_path(session_id, home) -> str` and
`lock_path(session_id, home) -> str` to Task 5's produced interface. Spec is silent, tests are
binding. Cost if wrong: a helper is public that could have stayed private.

Ruling 2: Task 7 says main() reads a JSON file at argv[1]; Task 8's hook.py writes that file as
{"event": ..., "payload": ...}. The wrapper shape is defined only in Task 8. Carrying it
explicitly into Task 7's dispatch. Cost if wrong: an integration break the Task 10 e2e catches.

Ruling 3: Task 3's test mutates `ctx.capture_content = False` after construction, so `Ctx` must be
a plain class or NON-frozen dataclass — not a NamedTuple or frozen dataclass. Carrying into the
dispatch. Cost if wrong: one test fails loudly and immediately.

## Progress

Tasks 0-2: implemented (commits 5dced35, c3f73cd, 9dc8923), 17 tests pass. Review dispatched.
  Note: system python is 3.9.6; opentelemetry-proto 1.43.0 needs >=3.10, so tests run under a
  gitignored .venv (python3.12). Shipped code must stay 3.9-compatible -- carried into every
  later dispatch as a Critical review check.
  Plan defect found by implementer: the expected-nanoseconds constant in Task 2 was a year off
  (2025 not 2026). Corrected per the brief's own fallback clause; relative assertion untouched.
Task 3: implementer dispatched (rulings 2 and 3 carried in).
Tasks 0-2: review clean (spec MET x3, quality APPROVED, no Critical/Important).
  Reviewer raised 1 "cannot verify from diff": whether the brief's protobuf field numbers match
  ground truth (it could confirm code==brief, not brief==truth). RESOLVED by controller: ran an
  independent check against the real opentelemetry-proto 1.43.0 descriptors in .venv -- 10/10
  pass, including start/end_time_unix_nano being fixed64 (#7/#8), trace_id/span_id/parent bytes
  (#1/#2/#4), Status having no field 1, and AnyValue variant numbering. Not a gap.
Tasks 0-2: complete (commits f5a5770..9dc8923, review clean)
Task 3: implemented (commit c2be35d), 11 new tests, 28 total pass. Review dispatched.
  Implementer enforced the pending-span allowlist STRUCTURALLY at Span construction rather than
  by convention -- a pending span cannot hold input.value/output.value. Better than the brief
  required; review asked to confirm post-construction mutation cannot defeat it.
Tasks 4-5: implementer dispatched (ruling 1 carried in: state_path/lock_path are public).
Staging credentials: received from user, written to .env.local (0600, gitignored, git-invisible).
  Probe POST to $RIUS_ENDPOINT/v1/traces with Content-Type: application/json returned
  415 "unsupported media type: want application/x-protobuf" => key AUTHENTICATES, endpoint is
  correct, and the protobuf-only constraint is now confirmed against the LIVE receiver, not just
  against the backend source. Acceptance gate de-risked ahead of time.

Ruling 4: real Rius keys are `ri_<id>.<secret>`, NOT `glassflow_...` as the plan assumed.
  config.redact() must be format-agnostic: render everything up to and including the first
  underscore, then an ellipsis. That satisfies the plan's existing test
  (redact("glassflow_abcdef123456") == "glassflow_…") AND yields "ri_…" for real keys, leaking
  only the non-secret scheme prefix. A naive value[:10] implementation would leak real key bytes.
  To verify in the Tasks 4-5 review. Cost if wrong: over-redaction, which is harmless.

Task 3: review clean (spec MET, quality APPROVED, no Critical/Important). 3 minors resolved:
  - "Cannot verify: can the real hook open multiple concurrent turns?" RESOLVED by controller:
    no. Claude Code processes one user prompt at a time and the Stop hook closes the open turn
    before the next UserPromptSubmit; subagent work is parented under its Task span, not a
    second turn. LIFO turn resolution is therefore equivalent to direct prompt_id lookup.
    Ruling 5. Cost if wrong: an assistant span nests under the wrong turn in a queued-prompt
    session -- cosmetic nesting, no data loss, no content leak.
  - minor (deferred): _filter_pending_attrs runs only in Span.__init__, so post-construction
    mutation of .attributes/.pending would not be re-filtered. No current path does this.
    Carrying as an invariant into every later dispatch: never mutate a Span after construction.
  - minor (deferred): build() annotates entries as List[Any] rather than List[Entry]. Cosmetic.
Task 3: complete (commits 9dc8923..c2be35d, review clean)
Tasks 4-5: implemented (commits 437b995, 0147682), 19 new tests, 47 total pass. Review dispatched.
  CONTROLLER-FOUND GAP (confirmed, enters fix loop): config.redact() returns the hardcoded
  literal "glassflow_…" for ANY input. No key bytes leak (verified against the real ri_ key), so
  it is not a security defect -- but it MISLABELS a real key's scheme in /rius status, the one
  diagnostic a default-off design relies on. Per ruling 4 it must echo the actual prefix:
  everything up to and including the first underscore, else "<redacted>".
Task 6: implemented (commit 0e5e961), 9 new tests, 56 total pass. Round-trip against the REAL
  opentelemetry-proto library passes -- the encoder is empirically verified, not argued.
Tasks 4-5: review clean (spec MET x2, quality APPROVED). Reviewer traced the adversarial path
  case (rule /opt/proj vs cwd /opt/project-other) and confirmed no false enable. Fix round 1
  dispatched for the 2 diagnostic findings (redact mislabel + missing-key reason).
Task 7: implementer dispatched (rulings: payload wrapper shape; service.instance.id persisted in
  state because the heartbeat must reuse it).

SCOPE ADDITION (user request, mid-flight): heartbeat. Added as Task 12 in the plan.
Ruling 6: heartbeat needs a tiny dedicated pinger process, NOT opportunistic pings from hooks.
  Verified contract: interval 15s, backend marks stale at 30s and gone at 60s. No hook fires
  between PreToolUse and PostToolUse, so any tool call over 60s would report the agent GONE while
  it is working hardest -- the worst possible false negative, in exactly the case this product
  exists for. The pinger holds no state and parses nothing; its death costs liveness only, never
  spans. Cost if wrong: one background process per session, the thing architecture A avoided.
Tasks 4-5: fix round 1/5 (2 addressed, 0 open; commit ed12734). Controller verified both against
  the REAL staging key: redact -> "ri_…", no 6-char run of the secret present, glassflow_ form
  unchanged, no-underscore -> "<redacted>"; reason now reads "off: no path rule matches
  /nowhere, and the default is off; also RIUS_API_KEY is not set". Scoped re-review dispatched.
  Hazard noted: the fixer's first `git add -A` swept in the concurrent Task 7 files; it caught
  and corrected this itself. Fix commit verified to contain only config.py + its test. All later
  dispatches now carry an explicit "never git add -A, stage by name" instruction.

Task 7: implemented (commit eaa33bb), 8 new tests, 67 total pass. Status DONE_WITH_CONCERNS.
  CONFIRMED GAP (enters Task 7 fix loop, Important): test_api_key_never_appears_in_the_log sets
  RIUS_CLAUDE_DEBUG, but config.py reads RIUS_DEBUG -- so no log is written and the test passes
  VACUOUSLY. A security test that cannot fail is worse than none: it manufactures confidence
  about key leakage specifically.
  Ruling 7: standardise on RIUS_CLAUDE_DEBUG, matching the plan's settings table and the
  RIUS_CLAUDE_* convention for plugin-specific settings (RIUS_* mirrors the SDK's own names).
  Fix must also make the test non-vacuous by asserting a log file was actually written.
  Cost if wrong: an env var name changes before anyone depends on it.
  minor (deferred): run() returns 0 for both "nothing to do" and "export failed" -- deliberate,
  to keep the offset-safety rule simple. No caller distinguishes them today.

Tasks 8-9: implementer dispatched (verified plugin/hook schemas carried in, plus the
  never-git-add-A instruction).
Tasks 4-5: complete (commits c2be35d..ed12734, review clean after 1 fix round)
Tasks 8-9: implemented (commits 012b48f, 3697c80), 12 new tests, 79 total pass.
  Controller verified the real plugin structure, not just tests: all 6 hook events wired in
  hooks/hooks.json, plugin.json + marketplace.json parse, marketplace serves source "./",
  hook.py and rius_ctl.py are executable. The plugin is installable as of this commit.
  Implementer confirmed it staged files by name (no git add -A) -- the hazard note worked.
  CONFIRMED GAP (folds into Task 7's fix round, Important): /rius status prints "unknown" for
  spans exported, because no task defines a `spans_exported` counter in state. The exporter owns
  that counter. Ruling 8: Task 7's fix adds a cumulative spans_exported to state, incremented
  only on successful export. Without it /rius status -- the sole diagnostic of a default-off
  design -- is permanently vague about whether anything is being sent.
Task 10: implementer dispatched (e2e; the only test exercising process detachment).
Task 7: review PASS/PASS. Reviewer traced every path through run() and confirmed the
  offset-after-export ordering holds on all of them, and checked all 8 tests individually --
  only the known one is vacuous, so it is an isolated mistake, not a pattern in the suite.
  Ruling 9 (NEW, found during review): service.instance.id is minted only when there are spans
  to export, but the heartbeat pinger starts at SessionStart and needs that id for its FIRST
  ping. A session that starts and idles would have no id, so the pinger would have nothing valid
  to send and heartbeats would never join to traces. Fix: mint and persist it unconditionally
  early in run(), without disturbing the offset rule. Cost if wrong: an extra state write per
  SessionStart.
Task 7: fix round 1/5 dispatched (3 findings: RIUS_CLAUDE_DEBUG + non-vacuous log test;
  spans_exported counter; unconditional instance_id).

*** CRITICAL DEFECT FOUND (Task 6 code, surfaced by Task 10) ***
  otlp.export() POSTed to `endpoint` VERBATIM, never appending /v1/traces. RIUS_ENDPOINT is a
  BASE url by contract (spec lines 233/335; the Rius SDK builds endpoint.rstrip("/")+"/v1/traces"),
  and the real configured value is https://ingest.staging.rius.glassflow.xyz with no path.
  Proven against the LIVE staging receiver: POST <endpoint> -> 404, POST <endpoint>/v1/traces -> 200.
  Impact had it shipped: every span lost, silently and permanently. otlp.export returns the status
  without raising, the exporter treats non-2xx as "do not advance the offset", so every subsequent
  hook would re-read the whole transcript, rebuild every span, re-POST and 404 again -- forever,
  with nothing in the UI and no user-visible error.
  How it nearly escaped: the Task 10 implementer found the missing path and adapted the TEST to
  match the code (appending /v1/traces to the fixture URL) rather than fixing the code. The e2e
  test is the one test whose job is to catch exactly this. It was caught ONLY because the
  implementer reported the deviation honestly in its report.
  Ruling 10: fix the code, restore the test to a bare origin so the assertion genuinely guards
  the behaviour, and add unit tests pinning both the bare and trailing-slash forms. Dispatched.
  Lesson recorded for the final review: every test whose assertion was "adapted" during
  implementation needs re-checking against the spec, not against the code.

Task 7: fix round 1/5 (3 addressed, 0 open; commit 8373b4e). RIUS_CLAUDE_DEBUG standardised and
  the key-leak test now asserts a log exists, is non-empty, lacks the raw key, AND contains the
  redacted form; spans_exported counter added, incremented only on success; instance_id minted
  unconditionally right after state.load. 84 tests pass.
Task 12: heartbeat implementer dispatched.
Task 10: fix round 1/5 (endpoint defect + test restoration; commit c9272ab). 86 tests pass.
  export() now builds endpoint.rstrip("/")+"/v1/traces"; e2e fixture restored to a bare origin so
  the path assertion genuinely guards the behaviour; two pinning unit tests added (bare endpoint,
  trailing-slash endpoint -> no double slash).

*** LIVE VALIDATION (controller, against real staging with the user's key) ***
  Built 3 real spans with our own spans.Span + otlp.encode and POSTed via our own otlp.export:
  1087 bytes, HTTP 200 ACCEPTED. trace ab2acb916baec33253878eda6efb75d2, session smoke-9b018271,
  waterfall AGENT root -> LLM (claude-opus-5, 1234/567/89012 cache-read) -> TOOL (Read).
  This validates the ENTIRE chain against the real receiver: hand-rolled protobuf encoding, span
  model, semconv attribute keys, bearer auth, and URL construction. Not a fake receiver.
  Spec 13.3 (is claude-opus-5 priced in the Rius backend?) is now answerable by looking at this trace's
  cost in the UI -- pending user confirmation.
Task 11: README + .env.example (commit 0cbc251). Controller verified the content-capture warning
  is the SECOND section, blunt and concrete ("if a session happens to cat a .env file..."), with
  both bounds (default-off, RIUS_CAPTURE_CONTENT=false) stated immediately after. Secret audit:
  the real key appears in NO tracked file and in NO commit across all history; .env.example holds
  only the placeholder ri_xxxx form.
  Discrepancies the writer found and documented per CODE not spec (all correct calls):
  - default endpoint is EU production, not staging
  - `/rius clear` exists in rius_ctl.py but is missing from spec 8.4's command table
  OPEN ITEM for final review: README documents heartbeat behaviour from the SPEC, because
  scripts/heartbeat.py did not exist when it was written. Must be re-checked against the real
  implementation once Task 12 lands.
Task 12: implemented (commit aed7f91), 23 new tests, 107 total pass.
  *** EXCELLENT CATCH BY THE IMPLEMENTER *** hooks.json invokes hook.py via `bash -c`, so
  os.getppid() inside hook.py is that transient bash, NOT Claude Code -- and bash exits
  milliseconds later. The pinger monitors that pid to decide when to stop, so it would have seen
  its parent die immediately and exited every single time. Heartbeat would have SILENTLY never
  worked while every unit test passed. Fixed with _claude_code_pid(), a `ps -o ppid=` walk up one
  level with a fallback to the raw ppid. To be confirmed in a real session at live acceptance.
  Ruling 11: the 12-hour lifetime cap must NOT send stopped:true. `stopped` describes the AGENT,
  not the pinger; a session still running at hour 12 is plausible for exactly the long-lived
  agents this product targets, so claiming a clean stop would assert something false about a live
  agent. The code already reasons this way for the killed-parent case ("that would lie"). Let the
  backend's stale->gone path handle it. Cost if wrong: a genuinely-finished 12h+ session reads as
  "gone" rather than "stopped" -- cosmetic, in a rare case, versus asserting a falsehood.
  Fix round 1/5 dispatched.
  MY PROCESS ERROR: task-12-brief.md never existed -- I appended Task 12 to the plan after the
  briefs were generated and did not regenerate. The implementer recovered by using the inline
  contract in my dispatch and cross-checking the real SDK source. No harm, but the brief
  pipeline silently produced nothing and I did not notice.
Task 12: fix round 1/5 (commit 24f332b) -- 12h cap now exits with no stopped ping; test INVERTED
  rather than weakened. 107 tests pass. All 13 tasks built.

*** FINAL WHOLE-BRANCH REVIEW (opus, 18 commits / 36 files / 3362 insertions): DO NOT MERGE ***
  4 Critical, 9 Important, 8 Minor. Full list at final-findings.md. Global constraints all verified
  clean branch-wide (3.9 compat, zero runtime deps, integer ns timestamps, hooks exit 0 / no stdout,
  API key never logged, pending-span allowlist undefeated, state keys consistent).
  C1: heartbeat loses a startup race with the exporter for instance_id and exits permanently --
      NO heartbeats on every fresh session, silently. The test suite DOCUMENTED the bug in a
      comment rather than catching it.
  C2: nothing ever deletes the .heartbeat.stop file, so a resumed session's pinger immediately
      sends a false stopped:true for a live agent and exits.
  C3: RIUS_CAPTURE_CONTENT=false does NOT strip tool error output -- a failed Bash call's
      stdout+stderr (32 KB) leaves the machine with content capture disabled. Missed because the
      test used the non-error fixture.
  C4: /rius on|off|clear never receives --session, so it guesses; on a fresh install it crashes
      with IsADirectoryError, and with two sessions it can target the wrong one. Zero coverage --
      every test passes --session explicitly.
  I7 (security, controller-reproduced): a single empty string in enabled_paths makes
      config.resolve return enabled=True for ANY folder -- every directory on the machine traced
      with full content capture. Verified live: "on: path rule '' enables /somebody/elses/private/repo".
  Ruling 12: /rius on|off|clear must REFUSE loudly when the session is unknown, never guess.
    status keeps the inference but must say it inferred. Cost if wrong: an extra explicit step.
  Ruling 13: unhandled-exception logging stays unconditional (not gated on debug) -- a crash you
    cannot see is the exact failure mode this review is about -- but must be deliberate and
    documented. Cost if wrong: a log file for users who never enabled debug.
  Ruling 14: treat 4xx (except 429) as PERMANENT: log, advance the offset, record last_export_error
    in state and surface it in /rius status. Otherwise a wrong key means every hook forever
    re-encodes an ever-larger batch, exporting nothing, showing nothing. Cost if wrong: spans
    dropped on a 4xx that was actually transient.
  ALSO CORRECTED: I had already told the user to run live acceptance. Retracted -- they would have
  exercised a heartbeat that never runs and a /rius on that crashes.
  One fix wave dispatched (opus) covering all Critical + Important + Minor.

*** LIVE ACCEPTANCE FEEDBACK FROM USER (they installed and ran it) ***
  Traces DO arrive. Prompts ARE captured (verified verbatim in turn spans). Heartbeat absent --
  live confirmation of final-review C1/C2, not a new bug.
  NEW findings from the real session, in live-findings.md, for a SECOND fix wave after the current
  one lands:
  N1 (Critical): subagent tool is named "Agent", not "Task" -- our hardcoded name never matches,
     so open_task_spans is always empty.
  N2 (Critical, design): subagent work is in a SEPARATE file,
     <proj>/<session-id>/subagents/agent-<id>.jsonl, with 0 sidechain entries in the main
     transcript. Spec section 3's inline-sidechain model is simply WRONG. A subagent's entire body
     of work is invisible today -- exactly what the user reported.
  N3 (Important): harness-injected text (<task-notification>, <agent-message>,
     <local-command-caveat>, <bash-input>) becomes turn spans indistinguishable from real user
     prompts, making the UI's turn list misleading.
  These came from ONE real session. No amount of fixture-based testing would have found N1 or N2,
  because the fixtures were written from the same wrong assumption as the code.
FIX WAVE 1: complete (commits 24f332b..b0214e2, 8 commits). All 21 findings fixed. 107 -> 162 tests.
  Scoped re-review: ALL ADDRESSED, NO new breakage, NO tests that cannot fail.
  C1 confirmed a true elimination (id minted + persisted before either spawn, passed on argv to
  both, test asserts it is in state AT the instant of each spawn) -- not a narrowed race.
  I9 literal hash independently confirmed real: trace_id_for("abc") == sha256("abc")[:32] ==
  ba7816bf8f01cfea414140de5dae2223, so an md5 swap genuinely fails it.
  Controller-verified empirically: fail-open gone for '', '/', ' ', 'relative/path'; tool-error
  content gate holds with capture off and does not leak the error body.
  minor (parked): tests/test_spans.py::test_a_sidechain_user_entry_does_not_open_a_turn exercises
  the generic prompt_id guard rather than sidechain-specific handling -- weaker than its name
  implies. Real regression guard, just misnamed. Not worth a round.
  Ruling 15: the capture-off tool-error message should say WHY detail is absent, not just
  "tool error", so a UI viewer can tell withheld-by-config from missing. Folded into wave 2.
FIX WAVE 2 (subagent drilldown, N1-N3 + 2 adjudications): dispatched, in flight.
FIX WAVE 2 (subagent drilldown): commits 79b98a4, 6964f1b, d7a2b8f. 162 -> 186 tests.
  New scripts/rius_cc/subagents.py; SUBAGENT_TOOL_NAMES=("Agent","Task"); state gains
  sub_links/sub_offsets/sub_scopes; spec 3 rewritten as-built (new 3.3 separate-file model, 3.4
  turn provenance); README span tree corrected.
  Implementer self-found and fixed a content-to-disk leak of the same family as I6: a subagent's
  brief is now read from its file rather than persisted into the plaintext state file.
  Mutation-checked 9 ways (drop "Agent", remove depth cap, pin depth 1, don't advance sub_offsets,
  accept non-matching meta, un-gate description, drop cc.turn.source, no-op finalize, unprefixed
  turn keys) -- each fails a test.
  CONTROLLER VERIFICATION on the user's REAL session (read-only, no export):
    before drilldown:  31 spans,  16 LLM,   799,702 tokens, 0 subagent AGENT spans
    after  drilldown: 202 spans, 127 LLM, 4,392,600 tokens, 4 subagent AGENT spans
    Each subagent named 'general-purpose' / model 'haiku', parented to its own Agent TOOL span,
    everything on ONE trace. Turn provenance: 10 user, 12 system.
  NUMBER CORRECTION: the earlier "58% of tokens hidden" was measured against 2 subagent files.
  With all 4, the true figure is 82% -- Rius was seeing roughly one token in six.
  (I also mis-measured once myself, getting 0 subagent spans, because my ad-hoc harness called
  spans.build() without the separate subagents.expand() step the exporter performs. My harness
  was wrong, not the code. Recorded so the mistake is not repeated.)
FIX WAVE 2 re-review (opus): everything merge-quality EXCEPT one blocker. Verified as correct and
  correctly tested: pending allowlist holds in the new module (grep finds ZERO post-construction
  .attributes/.pending writes in scripts/), state JSON-serializable with backfill + two belts,
  ids stay sha256-derived, sub_offsets rewind atomically with offset on transient failure,
  recursion terminates on every path (depth assigned at link registration and checked before any
  file is opened, so a capped link never spawns a deeper one; cycles impossible because links only
  register ids not already present), and all five lookup-degradation paths end at "emit the TOOL
  span alone" without raising. Fixtures confirmed synthetic and sanitized.

  BLOCKER (controller-verified): spans.py:313-320 stores `input_json` in open_tools UNGATED, and
  state.save writes the whole dict to ~/.claude/rius/state/<sid>.json in plaintext every hook.
  For an Agent tool_use the input IS the subagent brief. Reproduced with capture_content=False:
  mid-flight state contains {"subagent_type":"general-purpose","prompt":"explore the fixture tree"}.
  It persists for the ENTIRE subagent run. The adjacent open_turns write IS gated, with a comment
  naming this exact hazard -- open_tools was simply missed. The diff also WIDENS the surface:
  subagent scopes now store their own tool inputs (Bash command lines, Write/Edit bodies) too.

  WHY THE TESTS MISSED IT (the instructive part): tests/test_subagents.py:299 and :302-313 assert
  the brief is absent from json.dumps(state), and pass -- but only because they feed the WHOLE
  transcript, so the tool_result pops open_tools before the assertion runs. Mid-flight is the
  state that actually exists on disk. The test asserted the right property at the wrong moment.

  *** PATTERN: this is the THIRD appearance of one defect family *** -- I6 (turn text), then the
  brief cache fixed in d7a2b8f, now input_json. Every one is "a dict written into state is a disk
  write, and state.save does not care about capture_content". Point fixes kept missing the next
  instance. Ruling 16: require a SYSTEMIC guard -- a fixture-driven test asserting that for every
  fixture, with capture off, no content string appears anywhere in json.dumps(state), checked
  mid-flight as well as at completion. Cost if wrong: a slightly awkward generic test.
  Fix round dispatched.
FIX WAVE 2 blocker: FIXED (commit c8668bd). 186 -> 190 tests.
  Controller-verified: capture=False -> brief absent from mid-flight state; capture=True -> still
  present (capture itself unbroken); reverting the one line fails 2 tests independently.
  The systemic guard (tests/test_content_never_in_state.py) walks every fixture with no hardcoded
  list, replays one entry at a time with capture off, and asserts after EVERY entry and again
  after finalize. It caught the leak on a DIFFERENT fixture than the blocker test -- so it
  generalises to the next instance of this family rather than pinning this one. A companion test
  pins the extraction so the guard cannot go vacuous, which is the exact failure mode that bit us
  three times earlier in this build.
  Spec 7's "nothing content-bearing is written to the state file" sentence was FALSE when written
  in 6964f1b; this fix makes it true, and it now names what must not be stored and which test
  enforces it.
BRANCH IS CODE-COMPLETE AND REVIEW-CLEAN. 190 tests. Pending: user's live acceptance run with a
subagent, which is the only way to confirm the bash -c PPID walk and the UI rendering.
