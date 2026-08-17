"""
debug_injuries.py

One-off diagnostic to figure out the real column names and ID format used
by nflreadpy's load_injuries(), since the join in projection_model.py is
currently failing to match players (e.g. Tyreek Hill comes back null).

Run this locally and share the output — it'll tell us how to fix the join.
"""

import nflreadpy as nfl
import polars as pl

injuries = nfl.load_injuries(seasons=[2025])
print("Columns:", injuries.columns)
print("Row count:", injuries.height)

# Try to find Tyreek Hill under whatever the name column is actually called.
name_cols = [c for c in injuries.columns if "name" in c.lower()]
print("Name-like columns:", name_cols)

found = False
for col in name_cols:
    matches = injuries.filter(pl.col(col).str.contains("Tyreek", literal=True))
    if matches.height > 0:
        print(f"\nFound via column '{col}':")
        print(matches)
        found = True
        break

if not found:
    print("\nNo match found via name columns — printing first 5 rows to inspect schema:")
    print(injuries.head(5))