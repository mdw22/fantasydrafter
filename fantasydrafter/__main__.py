"""
compute_fantasy_points.py

Step 1 of the fantasy draft tool: pull historical player stats via nflreadpy
and compute Full PPR fantasy points per player, per season (and per game).

This is the foundation the projection model will build on — everything else
(rankings, tiers, VBD, live draft assistant) consumes this output.

NOTE: This has not been run/tested against live nflreadpy data (no network
access in the environment this was written in). Run locally and sanity-check
a few known players/seasons before trusting the output.

Usage:
    python compute_fantasy_points.py
"""

import nflreadpy as nfl
import polars as pl

# ---------------------------------------------------------------------------
# Scoring settings — Full PPR. Kept as a config dict so it's easy to expose
# as user-editable settings later (matches the "customizable league settings"
# requirement).
# ---------------------------------------------------------------------------
SCORING = {
    "passing_yards": 0.04,       # 1 pt per 25 yards
    "passing_tds": 4.0,
    "passing_interceptions": -2.0,
    "sack_fumbles_lost": -2.0,
    "passing_2pt_conversions": 2.0,

    "rushing_yards": 0.1,        # 1 pt per 10 yards
    "rushing_tds": 6.0,
    "rushing_fumbles_lost": -2.0,
    "rushing_2pt_conversions": 2.0,

    "receptions": 1.0,           # Full PPR
    "receiving_yards": 0.1,      # 1 pt per 10 yards
    "receiving_tds": 6.0,
    "receiving_fumbles_lost": -2.0,
    "receiving_2pt_conversions": 2.0,
}

# Skill positions this script handles. K and DEF/DST use entirely different
# stat structures (field goals, points allowed, etc.) and are NOT covered
# here — handle those separately.
SKILL_POSITIONS = ["QB", "RB", "WR", "TE"]


def load_historical_stats(max_seasons_back: int = 20) -> pl.DataFrame:
    """
    Load weekly player stats for as many seasons as nflreadpy has available,
    up to max_seasons_back. Requirements call for weighting as much history
    as possible, defaulting to a ~20-season window if a cutoff is needed.
    """
    current_season = nfl.get_current_season()
    seasons = list(range(current_season - max_seasons_back + 1, current_season + 1))

    stats = nfl.load_player_stats(seasons=seasons, summary_level="week")
    return stats


def compute_fantasy_points(stats: pl.DataFrame) -> pl.DataFrame:
    """
    Add a `fantasy_points` column to weekly stats using the Full PPR scoring
    config above. Missing stat columns are treated as 0 (some columns may not
    apply to all positions/seasons).
    """
    # Build the weighted-sum expression, skipping any scoring columns that
    # aren't present in this particular stats pull (schema can vary slightly
    # by season/version).
    available_cols = set(stats.columns)
    terms = []
    for col, weight in SCORING.items():
        if col in available_cols:
            terms.append(pl.col(col).fill_null(0) * weight)
        else:
            print(f"Warning: expected column '{col}' not found in stats — skipping.")

    fantasy_points_expr = terms[0]
    for term in terms[1:]:
        fantasy_points_expr = fantasy_points_expr + term

    return stats.with_columns(fantasy_points_expr.alias("fantasy_points"))


def build_season_summary(weekly: pl.DataFrame) -> pl.DataFrame:
    """
    Aggregate weekly fantasy points to season totals + per-game averages,
    filtered to skill positions. This is the shape the projection model
    (next step) will consume.
    """
    skill = weekly.filter(pl.col("position").is_in(SKILL_POSITIONS))

    season_summary = (
        skill.group_by(["player_id", "player_display_name", "position", "season"])
        .agg(
            [
                pl.len().alias("games_played"),
                pl.col("fantasy_points").sum().alias("fantasy_points_total"),
                pl.col("fantasy_points").mean().alias("fantasy_points_per_game"),
            ]
        )
        .sort(["season", "fantasy_points_total"], descending=[True, True])
    )
    return season_summary


def main():
    print("Loading historical player stats (this may take a while on first run)...")
    weekly_stats = load_historical_stats(max_seasons_back=20)

    print("Computing Full PPR fantasy points per game...")
    weekly_with_points = compute_fantasy_points(weekly_stats)

    print("Building season-level summary...")
    season_summary = build_season_summary(weekly_with_points)

    out_path = "fantasy_points_by_season.csv"
    season_summary.write_csv(out_path)
    print(f"Done. Wrote {season_summary.height} player-season rows to {out_path}")

    # Quick sanity check — top 10 player-seasons by total fantasy points,
    # across ALL seasons (not just the most recent one). This re-sorts by
    # points alone; season_summary itself stays sorted by season for the
    # CSV output since later steps will want it grouped that way.
    print("\nTop 10 player-seasons of all time (by total fantasy points):")
    print(season_summary.sort("fantasy_points_total", descending=True).head(10))


if __name__ == "__main__":
    main()