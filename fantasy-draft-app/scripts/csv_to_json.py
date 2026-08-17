"""
csv_to_json.py

Converts cheat_sheet.csv (from vbd_and_tiers.py) into the JSON file the
React app fetches at runtime (public/data/cheat_sheet.json). Run this
after regenerating cheat_sheet.csv and before `npm run dev` / `npm run
build`, so the app reflects your latest projections.

Usage (run from the fantasy-draft-app project root):
    python scripts/csv_to_json.py /path/to/cheat_sheet.csv

If no path is given, defaults to fantasydrafter/lib/cheat_sheet.csv
(vbd_and_tiers.py's actual output location) relative to the repo root.
"""

import json
import sys
from pathlib import Path

import polars as pl

# Only the columns the UI actually uses — keeps the JSON small and avoids
# shipping intermediate/debug columns (like raw age or roster status) to
# the public site.
COLUMNS_FOR_UI = [
    "player_id",
    "player_display_name",
    "position",
    "projected_fantasy_points",
    "vbd",
    "tier",
    "position_rank",
    "vbd_rank_overall",
    "adp_proxy_rank_overall",
    "vbd_vs_adp_proxy_overall",
]


def main():
    if len(sys.argv) > 1:
        csv_path = Path(sys.argv[1])
    else:
        csv_path = (
            Path(__file__).resolve().parent.parent.parent
            / "fantasydrafter" / "lib" / "cheat_sheet.csv"
        )

    if not csv_path.exists():
        print(f"Error: {csv_path} not found.")
        print("Pass the path explicitly: python scripts/csv_to_json.py /path/to/cheat_sheet.csv")
        sys.exit(1)

    df = pl.read_csv(csv_path)

    missing = [c for c in COLUMNS_FOR_UI if c not in df.columns]
    if missing:
        print(f"Warning: expected columns not found in {csv_path}: {missing}")

    available_cols = [c for c in COLUMNS_FOR_UI if c in df.columns]
    df = df.select(available_cols)

    out_path = Path(__file__).resolve().parent.parent / "public" / "data" / "cheat_sheet.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    records = df.to_dicts()
    out_path.write_text(json.dumps(records, indent=None))

    print(f"Wrote {len(records)} players to {out_path}")


if __name__ == "__main__":
    main()
