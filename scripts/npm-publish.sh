#!/usr/bin/env bash
set -euo pipefail

AUTH_TOKEN="${NPM_TOKEN:-${NODE_AUTH_TOKEN:-}}"
if [[ -n "$AUTH_TOKEN" ]]; then
  npm config set "//registry.npmjs.org/:_authToken=$AUTH_TOKEN" >/dev/null
fi

if ! npm whoami >/dev/null 2>&1; then
  echo "npm authentication missing; provide NPM_TOKEN/NODE_AUTH_TOKEN in CI or login locally" >&2
  exit 1
fi

PUBLISHED=$(npm view @kahinmcp/kahin version 2>/dev/null || echo none)
LOCAL=$(node -p "require('./package.json').version")
echo "npm: $PUBLISHED | local: $LOCAL"

if [ "$PUBLISHED" != "$LOCAL" ]; then
  npm publish --access public --provenance=false
  echo "published $LOCAL"
else
  echo "version unchanged — publish atlandı"
fi
