"""Replay a Cursor fixture's hook payloads through the real spool."""
import itertools
import json
import pathlib

from rius_cc import cursor_events

DIR = pathlib.Path(__file__).parent / "fixtures" / "cursor"
BASE_NS = 1790000000000000000
STEP_NS = 1000000000


def payloads(name):
    return json.loads((DIR / (name + ".json")).read_text(encoding="utf-8"))["events"]


def all_names():
    return sorted(p.stem for p in DIR.glob("*.json"))


def clock():
    """One second per hook, so every event has its own, known time."""
    counter = itertools.count()
    return lambda: BASE_NS + next(counter) * STEP_NS


def spool(name, spool_dir, capture_content=True, max_attr_bytes=32768):
    """Spool every payload of a fixture; returns its root conversation id."""
    tick = clock()
    events = payloads(name)
    for payload in events:
        cursor_events.record(payload, str(spool_dir), capture_content,
                             max_attr_bytes, clock=tick)
    return events[0]["conversation_id"]
