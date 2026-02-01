#!/bin/bash

SEMVER_REGEX='^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-((?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?(?:\+([0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?$'
UUID="289f771f-2c9a-4d73-9f3f-8492495a924d"

set -euo pipefail

echo "Searching for repositories..."

repositories=$(gh api \
  -X GET \
  search/code \
  -f q="\"${UUID}\" filename:tooth.json path:/" \
  -f per_page=100 \
  --jq '.items[].repository.full_name' \
  --paginate)

for repo in $repositories; do
  echo "Processing repository: $repo"

  tags=$(git ls-remote \
    -t \
    --refs \
    "https://github.com/${repo}.git" \
    | awk -F'/' '{print $3}' \
    || true)

  if [ -z "$tags" ]; then
    echo "  No tags found"
    continue
  fi

  for tag in $tags; do
    echo "  Fetching tag: $tag"

    if ! echo "$tag" | grep -Pq "$SEMVER_REGEX"; then
      echo "    Not SemVer"
      continue
    fi

    local_dir="./workspace/lipr/github.com/${repo}/${tag}"

    curl \
      -Lfs \
      -o "${local_dir}/tooth.json" \
      --create-dirs \
      https://raw.githubusercontent.com/${repo}/${tag}/tooth.json \
      || {
        echo "    Failed"
        rmdir "${local_dir}" 2>/dev/null || true
        continue
      }

    # Validate the fetched tooth.json.
    if ! grep -q "${UUID}" "${local_dir}/tooth.json"; then
      echo "    Invalid"
      rm -fr "${local_dir}" 2>/dev/null || true
      continue
    fi

    echo "    Success"
  done
done
