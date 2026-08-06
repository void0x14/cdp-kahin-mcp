#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT"

# This is intentionally a real runtime gate: no Playwright fallback and no
# skip-green behavior. Build the sidecar from the checkout before running
# pytest so a stale vendored binary cannot make source changes look tested.
ZIG_BIN="${KAHIN_ZIG_BIN:-}"
ZIG_TMP=""

cleanup_zig() {
  if [[ -n "$ZIG_TMP" ]]; then
    rm -rf -- "$ZIG_TMP"
  fi
}
trap cleanup_zig EXIT

if [[ -z "$ZIG_BIN" ]] && command -v zig >/dev/null 2>&1; then
  ZIG_CANDIDATE="$(command -v zig)"
  case "$($ZIG_CANDIDATE version)" in
    0.16.*) ZIG_BIN="$ZIG_CANDIDATE" ;;
    *) echo "ignoring incompatible system Zig; fetching pinned 0.16.x" ;;
  esac
fi

if [[ -z "$ZIG_BIN" ]]; then
  ZIG_VERSION="${KAHIN_ZIG_VERSION:-0.16.0}"
  case "$(uname -m)" in
    x86_64|amd64) ZIG_TARGET="x86_64-linux" ;;
    aarch64|arm64) ZIG_TARGET="aarch64-linux" ;;
    *) echo "error: unsupported Zig architecture: $(uname -m)" >&2; exit 1 ;;
  esac
  command -v curl >/dev/null 2>&1 || {
    echo "error: curl is required to fetch Zig $ZIG_VERSION" >&2
    exit 1
  }
  command -v tar >/dev/null 2>&1 || {
    echo "error: tar is required to unpack Zig $ZIG_VERSION" >&2
    exit 1
  }
  ZIG_TMP="$(mktemp -d)"
  ZIG_ARCHIVE="zig-${ZIG_TARGET}-${ZIG_VERSION}.tar.xz"
  curl --fail --silent --show-error --location --retry 3 --retry-all-errors \
    "https://ziglang.org/download/${ZIG_VERSION}/${ZIG_ARCHIVE}" \
    --output "$ZIG_TMP/$ZIG_ARCHIVE"
  tar -xJf "$ZIG_TMP/$ZIG_ARCHIVE" -C "$ZIG_TMP"
  ZIG_BIN="$ZIG_TMP/zig-${ZIG_TARGET}-${ZIG_VERSION}/zig"
fi

[[ -x "$ZIG_BIN" ]] || {
  echo "error: Zig executable is not usable: $ZIG_BIN" >&2
  exit 1
}

case "$($ZIG_BIN version)" in
  0.16.*) ;;
  *) echo "error: real E2E gate requires Zig 0.16.x" >&2; exit 1 ;;
esac

echo "==> running current Zig sidecar unit tests"
(cd "$ROOT/camoufox-harness/core" && "$ZIG_BIN" build test)
echo "==> building current Zig sidecar"
KAHIN_ZIG_BIN="$ZIG_BIN" bash scripts/build-sidecar.sh
SIDECAR="$ROOT/camoufox-harness/vendor/bin/kahin-sidecar"
[[ -x "$SIDECAR" ]] || {
  echo "error: sidecar build did not produce an executable: $SIDECAR" >&2
  exit 1
}

# The embedded Kahin sidecar and official Camoufox package are the same path
# used by the installed MCP server.
if command -v uv >/dev/null 2>&1; then
  uv sync --frozen --dev
  uv run python -m camoufox fetch
  CAMOUFOX_BIN="$(uv run python -c 'from kahin.the_twins.mirage import _camoufox_bin; print(_camoufox_bin())')"
  echo "==> validating Camoufox runtime: $CAMOUFOX_BIN"
  timeout 30s "$CAMOUFOX_BIN" --version
  KAHIN_REQUIRE_REAL_E2E=1 uv run pytest -q tests
else
  python3 -m pip install -e . pytest pytest-asyncio
  python3 -m camoufox fetch
  CAMOUFOX_BIN="$(python3 -c 'from kahin.the_twins.mirage import _camoufox_bin; print(_camoufox_bin())')"
  echo "==> validating Camoufox runtime: $CAMOUFOX_BIN"
  timeout 30s "$CAMOUFOX_BIN" --version
  KAHIN_REQUIRE_REAL_E2E=1 python3 -m pytest -q tests
fi
