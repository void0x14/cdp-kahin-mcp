#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT"

VERSION="${1:?usage: scripts/release-prepare.sh X.Y.Z}"
if [[ ! "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+([+-][0-9A-Za-z.-]+)?$ ]]; then
  echo "invalid release version: $VERSION" >&2
  exit 2
fi

python3 - "$VERSION" <<'PY'
import json
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

version = sys.argv[1]
root = Path.cwd()

package_path = root / "package.json"
package = json.loads(package_path.read_text())
package["version"] = version
package_path.write_text(json.dumps(package, indent=2) + "\n")

pyproject = root / "pyproject.toml"
text = pyproject.read_text()
text, count = re.subn(r'(?m)^version = "[^"]+"$', f'version = "{version}"', text, count=1)
if count != 1:
    raise SystemExit("could not update pyproject.toml version")
pyproject.write_text(text)

init = root / "kahin/__init__.py"
text = init.read_text()
text, count = re.subn(r'(?m)^__version__ = "[^"]+"$', f'__version__ = "{version}"', text, count=1)
if count != 1:
    raise SystemExit("could not update kahin.__version__")
init.write_text(text)

setup = root / "bin/setup.mjs"
text = setup.read_text()
text, count = re.subn(
    r'kahin-[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?-py3-none-any\.whl',
    f'kahin-{version}-py3-none-any.whl',
    text,
    count=1,
)
if count != 1:
    raise SystemExit("could not update embedded wheel reference")
setup.write_text(text)

changelog = root / "CHANGELOG.md"
log = changelog.read_text()
if f"## [{version}]" in log:
    raise SystemExit(f"CHANGELOG.md already contains {version}")

previous = re.search(r"^## \[([^]]+)\]", log, re.MULTILINE)
previous_version = previous.group(1) if previous else "0.0.0"
try:
    previous_tag = subprocess.check_output(
        ["git", "describe", "--tags", "--match", "v*", "--abbrev=0"],
        text=True,
        stderr=subprocess.DEVNULL,
    ).strip()
    subjects = subprocess.check_output(
        ["git", "log", "--format=- %s", f"{previous_tag}..HEAD"],
        text=True,
    ).strip()
except subprocess.CalledProcessError:
    subjects = subprocess.check_output(["git", "log", "-12", "--format=- %s"], text=True).strip()
subjects = subjects or "- Release metadata synchronized."

section = (
    f"## [{version}] — {date.today().isoformat()}\n\n"
    "### Değişen\n"
    f"{subjects}\n\n"
)
marker = "# Changelog\n\n"
if not log.startswith(marker):
    raise SystemExit("CHANGELOG.md does not start with the expected heading")
changelog.write_text(marker + section + log[len(marker):])

link = f"[{version}]: https://gitlab.com/void0x14/kahin-mcp/-/compare/v{previous_version}...v{version}"
if link not in log:
    changelog.write_text(changelog.read_text().rstrip() + "\n" + link + "\n")
PY

uv lock
uv build --wheel

wheel="dist/kahin-${VERSION}-py3-none-any.whl"
if [[ ! -f "$wheel" ]]; then
  echo "uv did not produce $wheel" >&2
  exit 1
fi

find lib -maxdepth 1 -type f -name 'kahin-*.whl' -delete
cp "$wheel" "lib/kahin-${VERSION}-py3-none-any.whl"
bash scripts/release-check.sh "$VERSION"
echo "release-prepare: ${VERSION} ready for review, commit, tag, and CI publish"
