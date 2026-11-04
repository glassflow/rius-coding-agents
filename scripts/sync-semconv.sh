#!/usr/bin/env bash
# Vendor rius-semconv's generated constants module into this plugin, at a
# pinned release tag, or check that the vendored copy still matches it.
#
#   TAG=v1.3.0 scripts/sync-semconv.sh     # vendor (or re-pin) at a tag
#   scripts/sync-semconv.sh --check        # exit 1 if the copy differs from its tag
#
# The copy is scripts/rius_cc/_semconv_generated.py: a two-line header naming
# the tag, then out/python/_semconv_generated.py from rius-semconv at that tag,
# byte for byte. --check reads the tag from the header. The module imports
# nothing but the standard library and runs on Python 3.9, so the plugin stays
# free of dependencies.
#
# The file is read from a rius-semconv checkout ($RIUS_SEMCONV, default
# ../rius-semconv) when there is one, else from GitHub with `gh api` (the
# repository is public; CI passes the job token as GH_TOKEN).
set -euo pipefail

ROOT="$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
SRC="${RIUS_SEMCONV:-$ROOT/../rius-semconv}"
DEST="$ROOT/scripts/rius_cc/_semconv_generated.py"
UPSTREAM="out/python/_semconv_generated.py"
HEADER_LINES=2

check=false
if [[ "${1:-}" == "--check" ]]; then
  check=true
  TAG="$(sed -n '1s/^# Vendored from rius-semconv \([^ ]*\) .*/\1/p' "$DEST")"
  if [[ -z "$TAG" ]]; then
    echo "no rius-semconv tag in the header of $DEST" >&2
    exit 1
  fi
elif [[ -z "${TAG:-}" ]]; then
  echo "set TAG to the rius-semconv release to vendor, e.g. TAG=v1.2.0" >&2
  exit 1
fi

upstream() {
  if git -C "$SRC" rev-parse --git-dir >/dev/null 2>&1; then
    git -C "$SRC" fetch --quiet --tags origin 2>/dev/null || true
    git -C "$SRC" show "refs/tags/$TAG:$UPSTREAM"
  else
    gh api -H "Accept: application/vnd.github.raw" \
      "repos/glassflow/rius-semconv/contents/$UPSTREAM?ref=refs/tags/$TAG"
  fi
}

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
upstream >"$WORK/upstream"

if $check; then
  tail -n +"$((HEADER_LINES + 1))" "$DEST" >"$WORK/vendored"
  if ! cmp -s "$WORK/upstream" "$WORK/vendored"; then
    diff -u "$WORK/upstream" "$WORK/vendored" | head -50 || true
    echo "scripts/rius_cc/_semconv_generated.py differs from rius-semconv $TAG $UPSTREAM." >&2
    echo "Re-vendor it with: TAG=$TAG scripts/sync-semconv.sh" >&2
    exit 1
  fi
  echo "scripts/rius_cc/_semconv_generated.py matches rius-semconv $TAG"
  exit 0
fi

{
  printf '# Vendored from rius-semconv %s %s by\n' "$TAG" "$UPSTREAM"
  printf '# scripts/sync-semconv.sh. Do not edit: change the registry in rius-semconv.\n'
  cat "$WORK/upstream"
} >"$DEST"
echo "vendored rius-semconv $TAG $UPSTREAM"
