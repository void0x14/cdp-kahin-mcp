#!/usr/bin/env bash
# Restore the Camoufox runtime from the GitLab Generic Package registry.
# The pipeline never talks to GitHub: the browser bundle (browsers/, fonts,
# addons, version metadata) is uploaded once to this project's package
# registry and every job restores it from there instead of running
# `python -m camoufox fetch`, which queries the GitHub API and trips its
# anonymous rate limit on shared runners.
set -euo pipefail

VERSION="${CAMOUFOX_BROWSER_VERSION:-152.0.4-beta.28-924f3109}"
CACHE_ROOT="${CAMOUFOX_CACHE_ROOT:-$HOME/.cache}"
CACHE_DIR="$CACHE_ROOT/camoufox"

if [[ -x "$CACHE_DIR/browsers/official/$VERSION/camoufox-bin" ]]; then
  echo "camoufox-cache: $VERSION already present at $CACHE_DIR"
  exit 0
fi

: "${CI_API_V4_URL:?CI_API_V4_URL is required}"
: "${CI_PROJECT_ID:?CI_PROJECT_ID is required}"

url="$CI_API_V4_URL/projects/$CI_PROJECT_ID/packages/generic/camoufox-browser/$VERSION/camoufox-cache.tar.xz"
archive="/tmp/camoufox-cache.tar.xz"
if [[ -n "${CI_JOB_TOKEN:-}" ]]; then
  if ! curl --fail --silent --show-error --location --retry 3 --retry-all-errors \
    --header "JOB-TOKEN: $CI_JOB_TOKEN" \
    "$url" --output "$archive"; then
    echo "camoufox-cache: JOB-TOKEN download failed; falling back to anonymous" >&2
    rm -f "$archive"
    curl --fail --silent --show-error --location --retry 3 --retry-all-errors \
      "$url" --output "$archive"
  fi
else
  curl --fail --silent --show-error --location --retry 3 --retry-all-errors \
    "$url" --output "$archive"
fi
mkdir -p "$CACHE_ROOT"
tar -xJf "$archive" -C "$CACHE_ROOT"
rm -f "$archive"
if [[ ! -x "$CACHE_DIR/browsers/official/$VERSION/camoufox-bin" ]]; then
  echo "camoufox-cache: restored bundle is missing $VERSION" >&2
  exit 1
fi
echo "camoufox-cache: restored $VERSION from the GitLab package registry"
