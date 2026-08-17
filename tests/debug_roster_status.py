"""
debug_roster_status.py

Inspect a player's week-by-week roster status across a season, to check
whether a "RES" (reserve) designation reflects a genuine season-changing
absence or just a late-season shutdown/rest (which shouldn't be treated
the same way — see the Saquon Barkley check this was written for).

Usage:
    python debug_roster_status.py "Tyreek Hill"
    python debug_roster_status.py "Saquon Barkley"
"""

import sys

import nflreadpy as nfl
import polars as pl

search_name = sys.argv[1] if len(sys.argv) > 1 else "Saquon Barkley"

rosters = nfl.load_rosters_weekly(seasons=[2025])
print("Columns:", rosters.columns)
print("Row count:", rosters.height)

name_cols = [c for c in rosters.columns if "name" in c.lower()]

found = False
for col in name_cols:
    matches = rosters.filter(pl.col(col).str.contains(search_name, literal=True))
    if matches.height > 0:
        print(f"\nFound via column '{col}' — showing all weeks for '{search_name}':")
        status_cols = [c for c in rosters.columns if "status" in c.lower()]
        week_col = "week" if "week" in rosters.columns else None
        cols_to_show = [c for c in [col, week_col] + status_cols if c]
        result = matches.select(cols_to_show)
        if week_col:
            result = result.sort(week_col)
        with pl.Config(tbl_rows=25):
            print(result)
        found = True
        break

if not found:
    print(f"\nNo match found for '{search_name}' — printing first 5 rows to inspect schema:")
    print(rosters.head(5))