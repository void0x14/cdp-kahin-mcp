#!/usr/bin/env bash
set -euo pipefail

query_err=$(mktemp)
trap 'rm -f "$query_err"' EXIT
set +e
PUBLISHED=$(npm view @kahinmcp/kahin version 2>"$query_err")
query_status=$?
set -e
if [ "$query_status" -ne 0 ]; then
  # A first publication legitimately returns 404.  Authentication, registry,
  # and network failures must stop the release; treating them as "unpublished"
  # can turn a transient outage into a misleading publish attempt.
  if grep -q "E404\|404 Not Found" "$query_err"; then
    PUBLISHED=none
  else
    cat "$query_err" >&2
    echo "npm registry query failed; release publication aborted" >&2
    exit "$query_status"
  fi
fi
LOCAL=$(node -p "require('./package.json').version")
echo "npm: $PUBLISHED | local: $LOCAL"

if [ "$PUBLISHED" = "$LOCAL" ]; then
  echo "version unchanged — publish atlandı"
  exit 0
fi

AUTH_TOKEN="${NPM_TOKEN:-${NODE_AUTH_TOKEN:-}}"
if [[ -n "$AUTH_TOKEN" ]]; then
  npm config set "//registry.npmjs.org/:_authToken=$AUTH_TOKEN" >/dev/null
fi

if ! npm whoami >/dev/null 2>&1; then
  echo "npm authentication missing; provide NPM_TOKEN/NODE_AUTH_TOKEN in CI or login locally" >&2
  exit 1
fi

npm publish --access public --provenance=false
echo "published $LOCAL"
