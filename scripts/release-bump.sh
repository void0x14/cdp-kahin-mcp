#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT"

DEFAULT_BRANCH="${CI_DEFAULT_BRANCH:-main}"
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
: "${GITLAB_PUSH_TOKEN:?GITLAB_PUSH_TOKEN is required for protected main/tag writes}"

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

wait_for_tag_pipeline() {
  local tag="$1" sha="$2" pipeline_id="" pipeline_status=""
  local api_root="https://${CI_SERVER_HOST:-gitlab.com}/api/v4/projects/${CI_PROJECT_ID}"

  for _ in $(seq 1 180); do
    pipeline_id=$(curl --fail --silent --show-error \
      "$api_root/pipelines?ref=$tag&sha=$sha&per_page=20" \
      | jq -r --arg tag "$tag" --arg sha "$sha" \
        'if type == "array" then ([.[] | select(.ref == $tag and .sha == $sha)] | .[0].id // empty) else empty end' \
      || true)
    if [[ -n "$pipeline_id" ]]; then
      break
    fi
    sleep 10
  done

  if [[ -z "$pipeline_id" ]]; then
    echo "release-bump: tag pipeline for $tag did not appear" >&2
    return 1
  fi

  for _ in $(seq 1 180); do
    pipeline_status=$(curl --fail --silent --show-error \
      "$api_root/pipelines/$pipeline_id" \
      | jq -r '.status // "unknown"' || true)
    case "$pipeline_status" in
      success)
        echo "release-bump: tag pipeline $pipeline_id is green; npm registry gate passed"
        return 0
        ;;
      failed|canceled|skipped|manual)
        echo "release-bump: tag pipeline $pipeline_id ended $pipeline_status" >&2
        return 1
        ;;
    esac
    sleep 10
  done

  echo "release-bump: tag pipeline $pipeline_id did not finish in time" >&2
  return 1
}

remote_tag_sha=$(git ls-remote origin "refs/tags/v${target_version}^{}" | awk 'NR == 1 { print $1 }')
if [[ -z "$remote_tag_sha" ]]; then
  remote_tag_sha=$(git ls-remote origin "refs/tags/v${target_version}" | awk 'NR == 1 { print $1 }')
fi
if [[ -n "$remote_tag_sha" ]]; then
  if [[ "$remote_tag_sha" == "$current_head" ]]; then
    wait_for_tag_pipeline "v${target_version}" "$current_head"
    exit $?
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

wait_for_tag_pipeline "v${target_version}" "$release_sha"
