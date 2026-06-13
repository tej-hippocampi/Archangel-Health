#!/usr/bin/env bash
# Package the Foundry submission into a single archive with a stable internal layout:
#   archangel_foundry_submission/{INDEX.md,README.md,instructions/*,data/*.csv}
set -euo pipefail
cd "$(dirname "$0")"

STAGE="$(mktemp -d)"
ROOT="$STAGE/archangel_foundry_submission"
mkdir -p "$ROOT/instructions" "$ROOT/data"

cp INDEX.md README.md "$ROOT/"
cp instructions/*.md "$ROOT/instructions/"
cp data/*.csv data/_manifest.json "$ROOT/data/"

OUT="$(pwd)/archangel_foundry_submission.zip"
rm -f "$OUT"
( cd "$STAGE" && zip -r -q "$OUT" archangel_foundry_submission )
rm -rf "$STAGE"

echo "built: $OUT"
unzip -l "$OUT"
