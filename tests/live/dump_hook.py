"""A Cursor hook for the `tokens` scenario: appends to the file named by
RIUS_LIVE_DUMP the key names of the payload on stdin, and its numeric values
(token counts). No text from the payload is kept. Answers `{}` so the hook is
a no-op for Cursor.
"""
import json
import os
import sys


def summary(payload):
    keys = sorted(payload) if isinstance(payload, dict) else []
    numbers = {k: v for k, v in payload.items()
               if isinstance(v, (int, float)) and not isinstance(v, bool)} if keys else {}
    return {"keys": keys, "numbers": numbers}


def main():
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        payload = {}
    target = os.environ.get("RIUS_LIVE_DUMP")
    if target:
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(summary(payload)) + "\n")
    print("{}")


if __name__ == "__main__":
    main()
