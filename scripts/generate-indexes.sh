#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PACKAGES_DIR="$ROOT_DIR/packages"
INDEX_FILE="$ROOT_DIR/index.json"
LEVILAUNCHER_FILE="$ROOT_DIR/levilauncher.json"
FILTER_FILE="$ROOT_DIR/scripts/generate-indexes.jq"
TMP_DIR="$(mktemp -d)"

cleanup() {
  rm -rf "$TMP_DIR"
}

trap cleanup EXIT

mapfile -d '' TOOTH_FILES < <(find "$PACKAGES_DIR" -type f -name "tooth.json" -print0 | sort -z)

if [ "${#TOOTH_FILES[@]}" -eq 0 ]; then
  echo "No tooth.json files found under $PACKAGES_DIR" >&2
  exit 1
fi

jq -s "." "${TOOTH_FILES[@]}" > "$TMP_DIR/tooths.json"

jq -n \
  --slurpfile existing_index "$INDEX_FILE" \
  --slurpfile existing_levi "$LEVILAUNCHER_FILE" \
  --slurpfile tooths "$TMP_DIR/tooths.json" \
  -f "$FILTER_FILE" \
  > "$TMP_DIR/generated.json"

jq -c ".index" "$TMP_DIR/generated.json" > "$INDEX_FILE"
jq -c ".levilauncher" "$TMP_DIR/generated.json" > "$LEVILAUNCHER_FILE"

echo "Generated index.json and levilauncher.json from ${#TOOTH_FILES[@]} tooth files."
