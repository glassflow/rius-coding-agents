"""What the live harness expects of the spans a Cursor run sent.

Pure: takes the lines of a spans.jsonl (OTLP/JSON bodies) and returns spans as
plain dicts, and for each scenario a list of problems (empty when it holds).
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

ERROR = 2
NO_SPAN = "no span reached the receiver"
CONTENT_KEYS = ("input.value", "output.value")
KIND = "openinference.span.kind"
PENDING = "rius.span.pending"
TOOL_NAME = "gen_ai.tool.name"
INPUT_TOKENS = "gen_ai.usage.input_tokens"
OUTPUT_TOKENS = "gen_ai.usage.output_tokens"


def _value(any_value: Dict[str, Any]) -> Any:
    if "intValue" in any_value:
        return int(any_value["intValue"])
    if "arrayValue" in any_value:
        return [_value(v) for v in any_value["arrayValue"].get("values", [])]
    for key in ("stringValue", "boolValue", "doubleValue", "bytesValue"):
        if key in any_value:
            return any_value[key]
    return None


def _attrs(items) -> Dict[str, Any]:
    return {item["key"]: _value(item.get("value") or {}) for item in items or []}


def decode(lines: List[str]) -> List[Dict[str, Any]]:
    """Every span in the bodies, oldest first, with its resource attributes."""
    out = []
    for line in lines:
        for resource_spans in json.loads(line).get("resourceSpans", []):
            resource = _attrs(resource_spans.get("resource", {}).get("attributes"))
            for scope in resource_spans.get("scopeSpans", []):
                for span in scope.get("spans", []):
                    out.append(_span(span, resource))
    return out


def _span(span: Dict[str, Any], resource: Dict[str, Any]) -> Dict[str, Any]:
    attrs = _attrs(span.get("attributes"))
    return {"name": span.get("name", ""), "trace_id": span.get("traceId", ""),
            "span_id": span.get("spanId", ""), "parent_id": span.get("parentSpanId", ""),
            "status": (span.get("status") or {}).get("code", 0),
            "attrs": attrs, "resource": resource, "kind": attrs.get(KIND, ""),
            "pending": bool(attrs.get(PENDING)), "events": span.get("events", [])}


def merge(spans: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """One span per id: a span is sent as pending while it runs and again when
    it finishes, and the finished one wins."""
    by_id: Dict[str, Dict[str, Any]] = {}
    for span in spans:
        seen = by_id.get(span["span_id"])
        if seen is None or not span["pending"] or seen["pending"]:
            by_id[span["span_id"]] = span
    return list(by_id.values())


def _of_kind(spans, kind):
    return [s for s in spans if s["kind"] == kind]


def _shells(spans):
    return [s for s in _of_kind(spans, "TOOL") if s["attrs"].get(TOOL_NAME) == "Shell"]


def _roots(spans):
    ids = {s["span_id"] for s in spans}
    return [s for s in spans if s["parent_id"] not in ids]


def common(spans, finished: bool = True) -> List[str]:
    if not spans:
        return [NO_SPAN]
    problems = []
    services = {s["resource"].get("service.name") for s in spans}
    if services != {"cursor"}:
        problems.append("service.name is %s, not cursor" % sorted(map(str, services)))
    roots = [s for s in _roots(spans) if s["kind"] == "AGENT"]
    if len(roots) != 1 or roots[0]["name"] != "cursor session":
        problems.append("expected one AGENT root `cursor session`, got %s"
                        % [(s["kind"], s["name"]) for s in _roots(spans)])
    elif finished and roots[0]["pending"]:
        problems.append("the root span never finished (no sessionEnd)")
    return problems


def _structure_only(spans) -> List[str]:
    leaked = sorted({k for s in spans for k in CONTENT_KEYS if k in s["attrs"]})
    return ["content attributes sent from a structure-only folder: %s" % leaked] if leaked else []


def check_shell(spans) -> List[str]:
    problems = common(spans) + _structure_only(spans)
    for kind in ("CHAIN", "LLM"):
        if not _of_kind(spans, kind):
            problems.append("no %s span" % kind)
    shells = _shells(spans)
    if len(shells) < 2:
        problems.append("expected two Shell TOOL spans, got %d" % len(shells))
    elif not any(s["status"] == ERROR for s in shells):
        problems.append("no Shell span is an ERROR (the `false` call)")
    elif all(s["status"] == ERROR for s in shells):
        problems.append("every Shell span is an ERROR (the `true` call should be OK)")
    return problems


def check_subagent(spans, finished: bool = True) -> List[str]:
    problems = common(spans, finished)
    tasks = {s["span_id"] for s in _of_kind(spans, "TOOL")
             if s["attrs"].get(TOOL_NAME) == "Task"}
    agents = [s for s in _of_kind(spans, "AGENT") if s["parent_id"] in tasks]
    if not tasks:
        problems.append("no Task TOOL span")
    elif not agents:
        problems.append("no AGENT span under the Task TOOL span")
    return problems


def check_interactive_tools(spans) -> List[str]:
    """An interactive session ends when the user quits, which may not be seen."""
    problems = check_subagent(spans, finished=False)
    if not _shells(spans):
        problems.append("no Shell TOOL span")
    return problems


def check_resume(spans) -> List[str]:
    problems = common(spans)
    if len({s["trace_id"] for s in spans}) > 1:
        problems.append("the resumed run started a new trace")
    roots = {s["span_id"] for s in _roots(spans) if s["kind"] == "AGENT"}
    turns = [s for s in _of_kind(spans, "CHAIN") if s["parent_id"] in roots]
    if len(turns) < 2:
        problems.append("expected a second turn (CHAIN span under the root) after --resume")
    return problems


def check_content(spans) -> List[str]:
    problems = common(spans)
    tools = _of_kind(spans, "TOOL")
    for key in CONTENT_KEYS:
        if not any(key in s["attrs"] for s in tools):
            problems.append("no TOOL span has %s" % key)
    return problems


def check_tokens(spans, required: bool) -> List[str]:
    llms = _of_kind(spans, "LLM")
    if not required:
        return [] if llms else ["no LLM span"]
    if not any(s["attrs"].get(INPUT_TOKENS, 0) > 0 and s["attrs"].get(OUTPUT_TOKENS, 0) > 0
               for s in llms):
        return ["no LLM span has gen_ai.usage.input_tokens and output_tokens above 0"]
    return []


def spool_events(lines: List[str]) -> List[str]:
    names = []
    for line in lines:
        try:
            names.append(json.loads(line).get("event", ""))
        except ValueError:
            continue
    return names


def check_interactive_events(names: List[str]) -> List[str]:
    return ["no %s event in the spool" % wanted
            for wanted in ("beforeSubmitPrompt", "stop") if wanted not in names]
