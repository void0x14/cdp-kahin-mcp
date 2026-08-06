#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT"

VERSION="${1:-$(node -p "require('./package.json').version")}"
export VERSION

python3 - "$VERSION" <<'PY'
import json
import re
import sys
from pathlib import Path

version = sys.argv[1]
if not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", version):
    raise SystemExit(f"invalid release version: {version}")

package_version = json.loads(Path("package.json").read_text())["version"]
if package_version != version:
    raise SystemExit(f"package.json: expected {version}, found {package_version}")

pyproject = Path("pyproject.toml").read_text()
if not re.search(rf"(?m)^version = \"{re.escape(version)}\"$", pyproject):
    raise SystemExit("pyproject.toml version is out of sync")

init = Path("kahin/__init__.py").read_text()
if f'__version__ = "{version}"' not in init:
    raise SystemExit("kahin.__version__ is out of sync")

setup = Path("bin/setup.mjs").read_text()
if f"kahin-{version}-py3-none-any.whl" not in setup:
    raise SystemExit("bin/setup.mjs points at a different wheel")

lock = Path("uv.lock").read_text()
match = re.search(r'(?ms)^\[\[package\]\]\nname = "kahin"\nversion = "([^"]+)"', lock)
if match is None or match.group(1) != version:
    found = match.group(1) if match else "missing"
    raise SystemExit(f"uv.lock: expected {version}, found {found}")

changelog = Path("CHANGELOG.md").read_text()
if f"## [{version}]" not in changelog:
    raise SystemExit(f"CHANGELOG.md has no {version} section")
PY

wheel="lib/kahin-${VERSION}-py3-none-any.whl"
if [[ ! -f "$wheel" ]]; then
  echo "missing embedded wheel: $wheel" >&2
  exit 1
fi

mapfile -t wheels < <(find lib -maxdepth 1 -type f -name 'kahin-*.whl' -printf '%f\n' | sort)
if [[ "${#wheels[@]}" -ne 1 || "${wheels[0]}" != "kahin-${VERSION}-py3-none-any.whl" ]]; then
  printf 'lib contains stale or missing wheels:\n%s\n' "${wheels[*]:-none}" >&2
  exit 1
fi

pack_json=$(mktemp)
trap 'rm -f "$pack_json"' EXIT
npm pack --dry-run --json >"$pack_json"
node - "$pack_json" "$wheel" <<'NODE'
const fs = require("node:fs");
const [jsonPath, wheel] = process.argv.slice(2);
const parsed = JSON.parse(fs.readFileSync(jsonPath, "utf8"));
const record = Array.isArray(parsed) ? parsed[0] : Object.values(parsed)[0] ?? parsed;
const files = record.files.map((entry) => entry.path);
if (!files.includes(wheel)) {
  console.error(`npm package does not contain ${wheel}`);
  process.exit(1);
}
NODE

echo "release-check: ${VERSION} is synchronized and npm-packable"
