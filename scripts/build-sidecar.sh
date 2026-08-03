#!/usr/bin/env bash
# Rebuild the Zig sidecar and vendor the binary.
# MCP runtime never needs zig — it uses camoufox-harness/vendor/bin/kahin-sidecar.
set -euo pipefail
cd "$(dirname "$0")/../camoufox-harness/core"

ZIG="${KAHIN_ZIG_BIN:-}"
if [[ -z "$ZIG" ]]; then
  if [[ -x "$HOME/.local/opt/zig-x86_64-linux-0.16.0/zig" ]]; then
    ZIG="$HOME/.local/opt/zig-x86_64-linux-0.16.0/zig"
  elif command -v zig >/dev/null 2>&1; then
    ZIG="$(command -v zig)"
  else
    echo "error: zig 0.16.x not found. Install it or set KAHIN_ZIG_BIN." >&2
    exit 1
  fi
fi

version="$("$ZIG" version)"
case "$version" in
  0.16.*) ;;
  *) echo "error: need zig 0.16.x, got $version (0.17 breaks the build). Set KAHIN_ZIG_BIN." >&2; exit 1 ;;
esac

echo "zig $version — building sidecar"
mkdir -p zig-out/bin
"$ZIG" build-exe --dep driver -Mroot=ipc_main.zig -Mdriver=driver.zig -O ReleaseSafe -femit-bin=zig-out/bin/kahin-sidecar
mkdir -p ../vendor/bin
cp zig-out/bin/kahin-sidecar ../vendor/bin/kahin-sidecar
echo "vendored: camoufox-harness/vendor/bin/kahin-sidecar"
