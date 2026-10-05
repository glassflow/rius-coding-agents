#!/bin/sh
# Find a Python and hand it mcp_headers.py: the `headersHelper` behind the
# bundled Rius MCP server (.mcp.json). Its stdout is a JSON object of HTTP
# headers, so with no interpreter it prints an empty object rather than
# prose -- the server then answers 401 and Claude Code says so.

# See hook.sh for why this is parameter expansion and has no `.` fallback.
case "$0" in
    */*)  dir=${0%/*} ;;
    *\\*) dir=${0%\\*} ;;
    *)    dir= ;;
esac

if [ -n "$dir" ] && [ -r "$dir/_find_python.sh" ]; then
    . "$dir/_find_python.sh"
fi

if [ -n "$dir" ] && [ -n "${rius_py:-}" ]; then
    exec "$rius_py" "$dir/mcp_headers.py" "$@"
fi
echo "{}"
