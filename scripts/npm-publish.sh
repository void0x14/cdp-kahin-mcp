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

if [[ -n "${CI_COMMIT_TAG:-}" && "$CI_COMMIT_TAG" != "v$LOCAL" ]]; then
  echo "release tag $CI_COMMIT_TAG does not match package version v$LOCAL" >&2
  exit 1
fi

verify_registry_package() {
  local package_dir archive manifest required attempt
  package_dir=$(mktemp -d)
  archive=""
  # The metadata API (npm view) propagates faster than the tarball CDN:
  # right after a publish, npm pack can still 404 the new version. Retry
  # instead of failing a successful release on CDN lag.
  for attempt in 1 2 3 4 5 6; do
    archive=$(npm pack "@kahinmcp/kahin@$LOCAL" --pack-destination "$package_dir" 2>"$query_err") && break
    sleep 10
  done
  if [[ -z "$archive" ]]; then
    tail -5 "$query_err" >&2
    echo "registry tarball for $LOCAL is not reachable yet; release not confirmed" >&2
    exit 1
  fi
  manifest="$package_dir/manifest"
  tar -tzf "$package_dir/$archive" >"$manifest"
  for required in \
    package/package.json \
    package/README.md \
    package/CHANGELOG.md \
    package/docs/juggler-ai-native.md \
    "package/lib/kahin-${LOCAL}-py3-none-any.whl"; do
    if ! grep -Fxq "$required" "$manifest"; then
      echo "registry package $LOCAL is missing $required" >&2
      exit 1
    fi
  done
  echo "registry package $LOCAL contains synchronized docs and wheel"
}

if [ "$PUBLISHED" = "$LOCAL" ]; then
  verify_registry_package
  echo "version unchanged — publish atlandı; registry already synchronized"
  exit 0
fi

AUTH_TOKEN="${NPM_TOKEN:-${NODE_AUTH_TOKEN:-}}"
if [[ -n "$AUTH_TOKEN" ]]; then
  npm config set "//registry.npmjs.org/:_authToken=$AUTH_TOKEN" >/dev/null
  if ! npm whoami >/dev/null 2>&1; then
    echo "configured NPM_TOKEN/NODE_AUTH_TOKEN was rejected by npm" >&2
    exit 1
  fi
elif [[ -n "${NPM_ID_TOKEN:-}" ]]; then
  echo "using npm GitLab OIDC trusted publishing"
else
  echo "npm authentication missing; configure NPM_ID_TOKEN trusted publishing or NPM_TOKEN/NODE_AUTH_TOKEN" >&2
  exit 1
fi

npm publish --access public

# npm registry propagation is normally quick but not instantaneous. Do not
# report a release as complete until a fresh registry read proves the exact
# local version is visible to consumers.
# npm's registry/CDN can take longer than the upload itself to expose the new
# version. Keep the publish job pending until an independent registry read
# proves synchronization, rather than turning a successful publish into a
# false-negative CI failure.
for attempt in 1 2 3 4 5 6 7 8 9 10 11 12; do
  PUBLISHED_AFTER=$(npm view @kahinmcp/kahin version 2>/dev/null || true)
  if [ "$PUBLISHED_AFTER" = "$LOCAL" ]; then
    verify_registry_package
    echo "published $LOCAL; registry synchronized"
    exit 0
  fi
  sleep 5
done

echo "npm registry did not expose $LOCAL after publish (found: ${PUBLISHED_AFTER:-none})" >&2
exit 1
