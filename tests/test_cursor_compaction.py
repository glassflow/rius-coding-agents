"""Cursor's preCompact hook: a rius.compaction event on the LLM span it
interrupted, and gen_ai.conversation.compacted on the next LLM span.

Cursor has no post-compaction hook and reports no size after, so the flag is
what tells a reader that a drop in context between two calls is a compaction.
The preCompact payload is the shape docs_session.json has (from Cursor's
hooks documentation: no real compaction has been captured), the rest are
synthetic turns around it.
"""
import json

import pytest

from rius_cc import cursor_events, cursor_export, cursor_spans, otlp, state
from rius_cc import _semconv_generated as semconv

from .cursor_fixtures import BASE_NS, STEP_NS, clock, payloads, spool

CID = "c0ffee00-0000-4000-8000-0000000000c1"
COMPACTED = "gen_ai.conversation.compacted"
EVENT = "rius.compaction"

DOCS_EVENT = {
    "rius.compaction.kind": "summarize",
    "rius.compaction.executor": "client",
    "rius.compaction.trigger": "auto",
    "rius.compaction.tokens.before": 108800,
    "rius.compaction.context_window": 128000,
    "rius.compaction.messages.compacted": 30,
    "rius.compaction.messages.kept": 15,
}


def _docs_precompact():
    return next(p for p in payloads("docs_session")
                if p["hook_event_name"] == "preCompact")


def _hook(event, gen=None, **fields):
    payload = {"conversation_id": CID, "hook_event_name": event,
               "cursor_version": "2026.08.27", "model": "gpt-5.1-codex"}
    if gen:
        payload["generation_id"] = gen
    payload.update(fields)
    return payload


def _compact(gen="gen-0001", **fields):
    payload = dict(_docs_precompact(), conversation_id=CID)
    payload.pop("generation_id")
    if gen:
        payload["generation_id"] = gen
    payload.update(fields)
    return payload


def _turn(gen, *compactions):
    """One turn: prompt, any preCompact hooks, an answer, stop."""
    return ([_hook("beforeSubmitPrompt", gen, prompt="work on it")]
            + list(compactions)
            + [_hook("afterAgentResponse", gen, text="done"),
               _hook("stop", gen, status="completed", loop_count=0)])


def _record(tmp_path, hooks, tick=None):
    tick = tick or clock()
    for payload in hooks:
        cursor_events.record(payload, str(tmp_path), True, 32768, clock=tick)


def _build(tmp_path, hooks, capture=True, final_ns=None):
    _record(tmp_path, hooks)
    events = cursor_events.read_conversation(str(tmp_path), CID)
    ctx = cursor_spans.Ctx(CID, capture, 32768)
    return cursor_spans.build(events, ctx, final_ns=final_ns)


def _llms(out):
    return [s for s in out if s.kind_oi == "LLM"]


def _flags(out):
    return [s.attributes.get(COMPACTED) for s in _llms(out)]


def _session(*turns):
    return ([_hook("sessionStart", session_id=CID)]
            + [hook for turn in turns for hook in turn]
            + [_hook("sessionEnd", reason="completed",
                     final_status="completed")])


def test_the_compaction_is_an_event_on_the_llm_span_it_interrupted(tmp_path):
    spool("docs_session", tmp_path)
    events = cursor_events.read_conversation(str(tmp_path),
                                             _docs_precompact()["conversation_id"])
    at = next(e["ts"] for e in events if e["event"] == "preCompact")
    out = cursor_spans.build(events, cursor_spans.Ctx(
        _docs_precompact()["conversation_id"], True, 32768))
    first, second = _llms(out)
    assert second.events == [(at, EVENT, DOCS_EVENT)]
    assert first.events == []
    # An event on the LLM span, not a span of its own.
    assert not [s for s in out if s.attributes.get(
        "gen_ai.operation.name") == "compaction"]
    assert [s.name for s in out if s.events and s.kind_oi != "TOOL"] == [
        "gpt-5.1-codex"]


def test_the_event_names_and_keys_are_the_vendored_semconv_ones():
    assert EVENT == semconv.RIUS_COMPACTION_EVENT
    assert set(DOCS_EVENT) <= {getattr(semconv, n) for n in dir(semconv)
                               if n.startswith("RIUS_COMPACTION_")
                               and isinstance(getattr(semconv, n), str)}


def test_the_context_keys_are_unchanged_beside_the_event(tmp_path):
    out = _build(tmp_path, _session(_turn("gen-0001", _compact())))
    llm = _llms(out)[0]
    assert llm.attributes["cursor.context.context_tokens"] == 108800
    assert llm.attributes["cursor.context.context_window_size"] == 128000
    percent = llm.attributes["cursor.context.context_usage_percent"]
    assert percent == 85 and isinstance(percent, float)
    assert len(llm.events) == 1


def test_the_next_llm_span_is_flagged_and_only_that_one(tmp_path):
    out = _build(tmp_path, _session(_turn("gen-0001", _compact("gen-0001")),
                                    _turn("gen-0002"), _turn("gen-0003")))
    assert _flags(out) == [None, True, None]
    assert [s.events for s in _llms(out)[1:]] == [[], []]


def test_a_size_drop_across_a_compaction_carries_the_flag(tmp_path):
    """Cursor sends the size before only: the flag on the next call is what
    marks the fall from 108800 as a compaction and not lost history."""
    out = _build(tmp_path, _session(_turn("gen-0001", _compact("gen-0001")),
                                    _turn("gen-0002")))
    before, after = _llms(out)
    assert before.attributes["cursor.context.context_tokens"] == 108800
    assert "cursor.context.context_tokens" not in after.attributes
    assert after.attributes[COMPACTED] is True
    assert not any(k.startswith("gen_ai.usage.") for k in after.attributes)


def test_the_compacting_turn_itself_is_not_flagged(tmp_path):
    out = _build(tmp_path, _session(_turn("gen-0001", _compact("gen-0001"))))
    assert _flags(out) == [None]


def test_with_no_later_llm_span_nothing_is_flagged(tmp_path):
    spool("docs_session", tmp_path)
    cid = _docs_precompact()["conversation_id"]
    out = cursor_spans.build(cursor_events.read_conversation(
        str(tmp_path), cid), cursor_spans.Ctx(cid, True, 32768))
    assert COMPACTED not in "".join(k for s in out for k in s.attributes)


def test_the_flag_waits_for_the_next_turn_to_have_an_llm_span(tmp_path):
    """The next turn is open and has said nothing: no LLM span yet, so the
    flag is not spent on it; it lands once the span exists."""
    hooks = (_session(_turn("gen-0001", _compact("gen-0001")))[:-1]
             + [_hook("beforeSubmitPrompt", "gen-0002", prompt="and next")])
    out = _build(tmp_path, hooks)
    assert _flags(out) == [None]
    later = hooks + [_hook("afterAgentResponse", "gen-0002", text="ok")]
    out = _build(tmp_path / "again", later)
    assert _flags(out) == [None, True]


def test_a_turn_without_an_llm_span_passes_the_flag_on(tmp_path):
    """A turn that was prompted and never answered has no LLM span to
    carry the flag, so the first span that exists after the compaction does."""
    asked = [_hook("beforeSubmitPrompt", "gen-0002", prompt="never answered")]
    hooks = (_turn("gen-0001", _compact("gen-0001")) + asked
             + _turn("gen-0003") + _turn("gen-0004"))
    out = _build(tmp_path, _session(hooks)[:-1])
    assert _flags(out) == [None, True, None]


def test_a_compaction_after_the_turn_stopped_still_lands_on_its_span(tmp_path):
    hooks = _turn("gen-0001") + [_compact("gen-0001")] + _turn("gen-0002")
    out = _build(tmp_path, _session(hooks))
    first, second = _llms(out)
    assert [e[1] for e in first.events] == [EVENT]
    assert _flags(out) == [None, True]


def test_a_late_compaction_does_not_use_up_the_flag_it_owes_the_next_call(
        tmp_path):
    # Turn 2 ran on the context turn 1 compacted, so it is flagged; the
    # compaction that reaches it after it stopped is owed to turn 3.
    hooks = (_turn("gen-0001", _compact("gen-0001")) + _turn("gen-0002")
             + [_compact("gen-0002")] + _turn("gen-0003"))
    out = _build(tmp_path, _session(hooks))
    assert _flags(out) == [None, True, True]
    assert [len(s.events) for s in _llms(out)] == [1, 1, 0]


def test_a_compaction_naming_no_turn_has_no_event_but_flags_the_next_call(
        tmp_path):
    hooks = _session(_turn("gen-0001"), [_compact(None)], _turn("gen-0002"),
                     _turn("gen-0003"))
    out = _build(tmp_path, hooks)
    assert all(not s.events for s in out)
    assert _flags(out) == [None, True, None]


def test_a_compaction_before_any_turn_flags_the_first_call(tmp_path):
    out = _build(tmp_path, _session([_compact(None)], _turn("gen-0001"),
                                    _turn("gen-0002")))
    assert _flags(out) == [True, None]


def test_a_subagents_compaction_is_not_ours(tmp_path):
    sub = "5ub00000-0000-4000-8000-0000000000c2"
    hooks = _session(_turn("gen-0001"),
                     [_compact("gen-0009", conversation_id=sub)],
                     _turn("gen-0002"))
    # Spooled under its own id, linked by the sidecar a parent link writes.
    cursor_export.link_subagent(str(tmp_path), sub, CID)
    out = _build(tmp_path, hooks)
    assert all(not s.events for s in out) and _flags(out) == [None, None]


def test_two_compactions_in_one_turn_are_two_events_and_one_flag(tmp_path):
    out = _build(tmp_path, _session(
        _turn("gen-0001", _compact("gen-0001"),
              _compact("gen-0001", context_tokens=120000,
                       is_first_compaction=False)),
        _turn("gen-0002"), _turn("gen-0003")))
    first = _llms(out)[0]
    assert [e[2]["rius.compaction.tokens.before"] for e in first.events] == [
        108800, 120000]
    assert [e[0] for e in first.events] == sorted(e[0] for e in first.events)
    assert first.attributes["cursor.context.context_tokens"] == 120000
    assert _flags(out) == [None, True, None]


def test_a_compaction_in_each_of_two_turns_flags_each_next_call(tmp_path):
    out = _build(tmp_path, _session(
        _turn("gen-0001", _compact("gen-0001")),
        _turn("gen-0002", _compact("gen-0002")),
        _turn("gen-0003"), _turn("gen-0004")))
    assert _flags(out) == [None, True, True, None]
    assert [len(s.events) for s in _llms(out)] == [1, 1, 0, 0]


def test_the_same_hook_run_twice_is_one_compaction(tmp_path):
    """The plugin's hook and an installed hooks.json both fire: an echo."""
    twice = [_compact("gen-0001"), _compact("gen-0001")]
    out = _build(tmp_path, _session(_turn("gen-0001", *twice)))
    assert len(_llms(out)[0].events) == 1


def test_a_replay_gives_the_same_ids_and_event_content(tmp_path):
    hooks = _session(_turn("gen-0001", _compact("gen-0001")),
                     _turn("gen-0002"))
    first = _build(tmp_path / "a", hooks)
    again = _build(tmp_path / "b", hooks)
    assert [(s.span_id, s.start_ns, s.end_ns, s.events, s.attributes)
            for s in first] == [
        (s.span_id, s.start_ns, s.end_ns, s.events, s.attributes)
        for s in again]
    rebuilt = cursor_spans.build(cursor_events.read_conversation(
        str(tmp_path / "a"), CID), cursor_spans.Ctx(CID, True, 32768))
    assert [s.events for s in rebuilt] == [s.events for s in first]


BAD = [True, False, -1, 1.5, "108800", None, [], {}, float("inf")]


@pytest.mark.parametrize("bad", BAD, ids=repr)
def test_a_malformed_number_is_left_out(tmp_path, bad):
    hooks = _session(_turn("gen-0001", _compact(
        context_tokens=bad, context_window_size=bad, message_count=bad,
        messages_to_compact=bad)))
    out = _build(tmp_path, hooks)
    event = _llms(out)[0].events[0][2]
    assert event == {"rius.compaction.kind": "summarize",
                     "rius.compaction.executor": "client",
                     "rius.compaction.trigger": "auto"}
    assert otlp.encode({}, out)


@pytest.mark.parametrize("field,key", [
    ("context_tokens", "tokens.before"),
    ("context_window_size", "context_window"),
    ("messages_to_compact", "messages.compacted"),
])
def test_each_number_is_sent_on_its_own(tmp_path, field, key):
    only = {f: None for f in ("context_tokens", "context_window_size",
                              "message_count", "messages_to_compact")}
    only[field] = 7
    event = _llms(_build(tmp_path, _session(_turn(
        "gen-0001", _compact(**only)))))[0].events[0][2]
    assert event["rius.compaction." + key] == 7
    assert not {k for k in event if k.startswith("rius.compaction.")
                } - {"rius.compaction." + key, "rius.compaction.kind",
                     "rius.compaction.executor", "rius.compaction.trigger"}


@pytest.mark.parametrize("total,compacted,kept", [
    (45, 30, 15), (30, 30, 0), (5, 30, None), (45, None, None),
    (None, 30, None), (45, -3, None), (True, 1, None),
])
def test_messages_kept_is_the_difference_when_it_is_known(
        tmp_path, total, compacted, kept):
    out = _build(tmp_path, _session(_turn("gen-0001", _compact(
        message_count=total, messages_to_compact=compacted))))
    event = _llms(out)[0].events[0][2]
    assert event.get("rius.compaction.messages.kept") == kept
    assert type(event.get("rius.compaction.messages.kept", 0)) is int


@pytest.mark.parametrize("trigger,sent", [
    ("auto", "auto"), ("manual", "manual"), ("Auto", None), ("timer", None),
    ("", None), (None, None), (True, None), (["auto"], None),
])
def test_the_trigger_is_auto_or_manual_or_absent(tmp_path, trigger, sent):
    out = _build(tmp_path, _session(_turn("gen-0001", _compact(
        trigger=trigger))))
    assert _llms(out)[0].events[0][2].get("rius.compaction.trigger") == sent


def test_a_bare_precompact_is_a_compaction_with_no_numbers(tmp_path):
    bare = _hook("preCompact", "gen-0001")
    out = _build(tmp_path, _session(_turn("gen-0001", bare), _turn("gen-0002")))
    assert _llms(out)[0].events[0][2] == {
        "rius.compaction.kind": "summarize",
        "rius.compaction.executor": "client"}
    assert _flags(out) == [None, True]
    assert not any(k.startswith("cursor.context.")
                   for k in _llms(out)[0].attributes)


def test_precompact_carries_no_content_into_the_spool_or_the_event(tmp_path):
    secret = "SECRET-text-in-an-unexpected-field"
    stray = _compact(summary=secret, prompt=secret, text=secret)
    for payload in _session(_turn("gen-0001", stray)):
        cursor_events.record(payload, str(tmp_path / "nocontent"), False,
                             32768, clock=clock())
    events = cursor_events.read_conversation(str(tmp_path / "nocontent"), CID)
    assert secret not in json.dumps(
        [e for e in events if e["event"] == "preCompact"])
    llm = _llms(cursor_spans.build(
        events, cursor_spans.Ctx(CID, False, 32768)))[0]
    assert secret not in json.dumps([llm.events, llm.attributes], default=str)
    assert set(llm.events[0][2]) == set(DOCS_EVENT)


def test_every_precompact_field_is_classified_as_identity_not_content():
    fields = set(_docs_precompact()) - {
        "conversation_id", "generation_id", "cursor_version", "model",
        "user_email", "transcript_path", "workspace_roots",
        "hook_event_name"}
    assert fields == {"trigger", "context_usage_percent", "context_tokens",
                      "context_window_size", "message_count",
                      "messages_to_compact", "is_first_compaction"}
    assert fields <= set(cursor_events.IDENTITY_FIELDS)
    assert not fields & set(cursor_events.CONTENT_FIELDS)


def _export(st, tmp_path, final_ns=None):
    events = cursor_events.read_conversation(str(tmp_path), CID)
    return cursor_export.spans_to_send(st, events, CID, True, 32768, final_ns)


def _spool_more(tmp_path, hooks, offset):
    tick = clock(BASE_NS + offset * STEP_NS)
    _record(tmp_path, hooks, tick)


def test_batched_exports_put_the_event_and_the_flag_on_the_right_spans(
        tmp_path):
    """Exports happen at stop and every 20 tools, never at preCompact: a
    span sent before its compaction is sent again with the event, and the
    next turn's span goes out flagged, once."""
    st = state.new_state()
    turn1 = _turn("gen-0001", _compact("gen-0001"))
    _spool_more(tmp_path, [_hook("sessionStart", session_id=CID)] + turn1[:1]
                + [_hook("afterAgentResponse", "gen-0001", text="part")], 0)
    sent = _export(st, tmp_path)
    assert [s.events for s in _llms(sent)] == [[]]

    _spool_more(tmp_path, turn1[1:], 10)
    sent = _export(st, tmp_path)
    llm = _llms(sent)[0]
    assert [e[1] for e in llm.events] == [EVENT] and COMPACTED not in llm.attributes

    _spool_more(tmp_path, _turn("gen-0002"), 20)
    sent = _export(st, tmp_path)
    second = _llms(sent)
    assert len(second) == 1 and second[0].attributes[COMPACTED] is True
    assert second[0].events == []
    assert _export(st, tmp_path) == []

    _spool_more(tmp_path, _turn("gen-0003"), 30)
    third = _llms(_export(st, tmp_path))
    assert len(third) == 1 and COMPACTED not in third[0].attributes


def test_an_event_alone_is_enough_to_send_a_span_again(tmp_path):
    """A preCompact with no numbers adds nothing to the span but the event."""
    st = state.new_state()
    turn = _turn("gen-0001")
    _spool_more(tmp_path, turn[:2], 0)
    assert [s.events for s in _llms(_export(st, tmp_path))] == [[]]
    _spool_more(tmp_path, [_hook("preCompact", "gen-0001")], 10)
    resent = _llms(_export(st, tmp_path))
    assert [[e[1] for e in s.events] for s in resent] == [[EVENT]]


def test_one_export_for_everything_gives_the_same_spans_as_many(tmp_path):
    hooks = _session(_turn("gen-0001", _compact("gen-0001")),
                     _turn("gen-0002"))
    _record(tmp_path / "all", hooks)
    whole = {s.span_id: s for s in _export(state.new_state(), tmp_path / "all")}
    st, seen = state.new_state(), {}
    tick = clock()
    for payload in hooks:
        cursor_events.record(payload, str(tmp_path / "step"), True, 32768,
                             clock=tick)
        seen.update({s.span_id: s for s in _export(st, tmp_path / "step")})
    assert {i: (s.events, s.attributes) for i, s in seen.items()} == {
        i: (s.events, s.attributes) for i, s in whole.items()}


def test_the_event_reaches_otlp_json_on_the_llm_span(tmp_path):
    out = _build(tmp_path, _session(_turn("gen-0001", _compact("gen-0001")),
                                    _turn("gen-0002")))
    body = json.loads(otlp.encode({"service.name": "cursor"}, out))
    spans = body["resourceSpans"][0]["scopeSpans"][0]["spans"]
    llm = [s for s in spans if s["name"] == "gpt-5.1-codex"]
    [event] = llm[0]["events"]
    assert event["name"] == EVENT and event["timeUnixNano"].isdigit()
    attrs = {a["key"]: a["value"] for a in event["attributes"]}
    assert attrs["rius.compaction.tokens.before"] == {"intValue": "108800"}
    assert attrs["rius.compaction.kind"] == {"stringValue": "summarize"}
    assert "events" not in llm[1]
    assert {"key": COMPACTED, "value": {"boolValue": True}} in llm[1]["attributes"]
