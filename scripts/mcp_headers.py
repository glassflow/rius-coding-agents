#!/usr/bin/env python3
"""headersHelper for the bundled Rius MCP server.

Prints the Authorization header for the same key tracing uses -- the one
`/rius login` stored, or RIUS_API_KEY -- so one sign-in serves both. With no
key it prints `{}` and the server's 401 tells the user to sign in.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rius_cc import config, platform_compat  # noqa: E402


def headers(env, home):
    api_key = config.resolve("", "", env, home).api_key
    return {"Authorization": "Bearer " + api_key} if api_key else {}


def main():
    try:
        out = headers(os.environ, platform_compat.home_dir(os.environ))
    except Exception:  # a traceback on stdout is not JSON; fail closed
        out = {}
    print(json.dumps(out))


if __name__ == "__main__":
    main()
