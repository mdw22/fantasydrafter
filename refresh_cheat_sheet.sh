#!/bin/bash
# Reruns the full pipeline and updates the React app's data in one shot:
#   1. fantasydrafter/__main__.py --force  (recompute everything)
#   2. fantasy-draft-app/scripts/csv_to_json.py  (regenerate cheat_sheet.json)
#
# Usage:
#   ./refresh_cheat_sheet.sh          # --force everything (default)
#   ./refresh_cheat_sheet.sh --no-force   # use cached CSVs where they exist
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

FORCE_FLAG="--force"
if [[ "${1:-}" == "--no-force" ]]; then
    FORCE_FLAG=""
fi

echo "== Step 1/2: running pipeline (fantasydrafter) =="
cd "$REPO_ROOT/fantasydrafter"
python3 __main__.py $FORCE_FLAG

echo
echo "== Step 2/2: regenerating cheat_sheet.json (fantasy-draft-app) =="
cd "$REPO_ROOT/fantasy-draft-app"
python3 scripts/csv_to_json.py

echo
echo "Done. cheat_sheet.json is up to date."
