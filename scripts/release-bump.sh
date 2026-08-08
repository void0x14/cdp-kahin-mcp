#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT"

DEFAULT_BRANCH="${CI_DEFAULT_BRANCH:-main}"
MODE="${RELEASE_BUMP_MODE:-commit-tag}"
if [[ "$MODE" != "prepare" && "$MODE" != "commit-tag" ]]; then
  echo "release-bump: unsupported mode $MODE" >&2
  exit 2
fi

if [[ "${CI_COMMIT_BRANCH:-}" != "$DEFAULT_BRANCH" ]]; then
  echo "release-bump: not the default branch; nothing to do"
  exit 0
fi

if [[ "${CI_COMMIT_MESSAGE:-}" =~ ^chore\(release\):\ v ]]; then
  echo "release-bump: release commit; tag pipeline owns publication"
  exit 0
fi

: "${CI_PROJECT_PATH:?CI_PROJECT_PATH is required}"
: "${CI_PROJECT_ID:?CI_PROJECT_ID is required}"
if [[ "$MODE" == "commit-tag" ]]; then
  : "${GITLAB_PUSH_TOKEN:?GITLAB_PUSH_TOKEN is required for protected main/tag writes}"
fi

git config user.name "Kahin Release Bot"
git config user.email "kahin-release-bot@noreply.gitlab.com"
git fetch origin "$DEFAULT_BRANCH" --tags --force

remote_head=$(git rev-parse "refs/remotes/origin/$DEFAULT_BRANCH")
current_head=$(git rev-parse HEAD)
if [[ "$remote_head" != "$current_head" ]]; then
  echo "release-bump: main advanced while this pipeline was running; newer pipeline owns release"
  exit 0
fi

current_version=$(node -p "require('./package.json').version")
latest_tag=$(git tag --list 'v[0-9]*.[0-9]*.[0-9]*' --sort=-version:refname | head -n 1 || true)
latest_version="${latest_tag#v}"

target_version=$(python3 - "$current_version" "$latest_version" <<'PY'
import sys

current, latest = sys.argv[1:]

def parse(value: str) -> tuple[int, int, int]:
    parts = value.split(".")
    if len(parts) != 3 or any(not part.isdigit() for part in parts):
        raise SystemExit(f"release-bump: unsupported version {value!r}")
    return tuple(map(int, parts))

current_tuple = parse(current)
latest_tuple = parse(latest) if latest else None
if latest_tuple is None or current_tuple > latest_tuple:
    print(current)
elif current_tuple == latest_tuple:
    major, minor, patch = current_tuple
    print(f"{major}.{minor}.{patch + 1}")
else:
    raise SystemExit(
        f"release-bump: package version {current} regresses behind tag {latest}"
    )
PY
)

remote_tag_sha=$(git ls-remote origin "refs/tags/v${target_version}^{}" | awk 'NR == 1 { print $1 }')
if [[ -z "$remote_tag_sha" ]]; then
  remote_tag_sha=$(git ls-remote origin "refs/tags/v${target_version}" | awk 'NR == 1 { print $1 }')
fi
if [[ -n "$remote_tag_sha" ]]; then
  if [[ "$remote_tag_sha" == "$current_head" ]]; then
    printf '%s\n' "$target_version" > release-version.txt
    echo "release-bump: v${target_version} already points at current main"
    exit 0
  fi
  echo "release-bump: v${target_version} already exists at another commit" >&2
  exit 1
fi

needs_prepare=0
if [[ "$current_version" != "$target_version" ]]; then
  needs_prepare=1
elif ! grep -Fqx "## [${target_version}]" CHANGELOG.md \
  && ! grep -Fq "## [${target_version}] —" CHANGELOG.md; then
  needs_prepare=1
elif ! grep -Fq "kahin-${target_version}-py3-none-any.whl" bin/setup.mjs; then
  needs_prepare=1
elif [[ ! -f "lib/kahin-${target_version}-py3-none-any.whl" ]]; then
  needs_prepare=1
fi

if [[ "$needs_prepare" -eq 1 ]]; then
  bash scripts/release-prepare.sh "$target_version"
fi
bash scripts/release-check.sh "$target_version"

if [[ "$MODE" == "prepare" ]]; then
  printf '%s\n' "$target_version" > release-version.txt
  echo "release-bump: prepared v${target_version} for npm publish and commit/tag"
  exit 0
fi

git add \
  package.json \
  pyproject.toml \
  uv.lock \
  kahin/__init__.py \
  bin/setup.mjs \
  CHANGELOG.md \
  "lib/kahin-${target_version}-py3-none-any.whl"

if ! git diff --cached --quiet; then
  git commit -m "chore(release): v${target_version}"
fi

push_url="https://oauth2:${GITLAB_PUSH_TOKEN}@${CI_SERVER_HOST:-gitlab.com}/${CI_PROJECT_PATH}.git"
git push "$push_url" "HEAD:${DEFAULT_BRANCH}"

release_sha=$(git rev-parse HEAD)
if git show-ref --verify --quiet "refs/tags/v${target_version}"; then
  local_tag_sha=$(git rev-list -n 1 "v${target_version}")
  if [[ "$local_tag_sha" != "$release_sha" ]]; then
    echo "release-bump: local v${target_version} points at another commit" >&2
    exit 1
  fi
else
  git tag -a "v${target_version}" -m "Release v${target_version}"
fi
git push "$push_url" "refs/tags/v${target_version}"
unset push_url GITLAB_PUSH_TOKEN
echo "release-bump: committed and tagged v${target_version}; tag pipeline will verify npm"
