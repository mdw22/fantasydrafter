#!/bin/bash
# Backtests the projection model against past seasons: for each year,
# projects using only data from before it, then checks that projection
# against what actually happened. Requires fantasy_points_by_season.csv
# to already exist (run ./refresh_cheat_sheet.sh at least once first).
#
# Usage:
#   ./run_backtest.sh                              # last 5 completed seasons
#   ./run_backtest.sh --years 2021,2022,2023,2024,2025
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

cd "$REPO_ROOT/fantasydrafter"
python3 -m bin.backtest "$@"
