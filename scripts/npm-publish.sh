#!/usr/bin/env bash
set -euo pipefail

npm config set "//registry.npmjs.org/:_authToken=${NPM_TOKEN}"

PUBLISHED=$(npm view @kahinmcp/kahin version 2>/dev/null || echo none)
LOCAL=$(node -p "require('./package.json').version")
echo "npm: $PUBLISHED | local: $LOCAL"

if [ "$PUBLISHED" != "$LOCAL" ]; then
  npm publish --provenance=false
  echo "published $LOCAL"
else
  echo "version unchanged — publish atlandı"
fi
