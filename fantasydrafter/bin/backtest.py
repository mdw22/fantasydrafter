"""
backtest.py

Validates the projection model's actual predictive accuracy. For each
past season Y, builds projections using ONLY data available before Y —
by calling the exact same functions projection_model.py's real pipeline
uses (recency-weighted average, age adjustment, currently-rostered
filter, roster-status exclusion), just parameterized by a historical
year instead of CURRENT_SEASON_OVERRIDE — then compares those
projections against what players actually scored in season Y.

Reuses projection_model.py's functions rather than reimplementing the
model, so backtest results always reflect whatever the real pipeline
currently does, not a separate copy that can drift out of sync.

Does NOT run VBD/tiers (bin/vbd_and_tiers.py) — those are draft-strategy
overlays with no ground truth to check against; this only validates the
underlying point/rank predictions.

Network-dependent per-year data (ages, rostered ids, roster status) is
split out into load_year_context() separately from the config-dependent
scoring step, so bin/grid_search.py can fetch each year's context ONCE
and reuse it across many config variations instead of re-hitting
nflreadpy for every grid point.

Usage:
    python -m bin.backtest                        # last 5 completed seasons
    python -m bin.backtest --years 2021,2022,2023,2024,2025
"""

import argparse
from pathlib import Path

import polars as pl

from bin.projection_model import (
    load_season_summary,
    load_player_ages,
    recency_weighted_projection,
    apply_age_adjustment,
    load_currently_rostered_ids,
    load_current_roster_status,
    filter_excluded_roster_status,
    add_position_rank,
)
from lib.config import CURRENT_SEASON_OVERRIDE, LOOKBACK_SEASONS, DECAY, AGE_CURVES, AGE_TREND_DAMPENING

POSITIONS = ["QB", "RB", "WR", "TE"]


def load_year_context(year: int) -> dict:
    """
    Network-dependent data for a backtest year that does NOT change
    across config variations (ages, rostered ids, roster status) — fetch
    once per year and reuse across as many scoring calls as needed.
    """
    return {
        "ages": load_player_ages(year),
        "rostered_ids": load_currently_rostered_ids(year),
        "roster_status": load_current_roster_status(year),
    }


def build_backtest_projection(
    season_summary: pl.DataFrame,
    year: int,
    year_context: dict,
    *,
    lookback_seasons: int = LOOKBACK_SEASONS,
    decay: float = DECAY,
    age_curves: dict = AGE_CURVES,
    age_trend_dampening: float = AGE_TREND_DAMPENING,
    verbose: bool = True,
) -> pl.DataFrame:
    """
    Rebuilds what projection_model.py would have projected for `year`,
    using only data from before it. Mirrors projection_model.main()'s
    steps (minus ADP proxy, which needs no validation here).

    lookback_seasons/decay/age_curves/age_trend_dampening default to
    module-level config but can be overridden — used by
    bin/grid_search.py to test alternate values. verbose=False silences
    the per-call roster-status drop count, which would otherwise print
    once per grid point across a search.
    """
    projections = recency_weighted_projection(season_summary, year, lookback_seasons, decay)
    projections = apply_age_adjustment(projections, year_context["ages"], age_curves, age_trend_dampening)

    rostered_ids = year_context["rostered_ids"]
    if rostered_ids is not None:
        projections = projections.filter(pl.col("player_id").is_in(list(rostered_ids)))

    projections = filter_excluded_roster_status(projections, year_context["roster_status"], verbose=verbose)
    projections = add_position_rank(projections)
    return projections


def score_year(
    season_summary: pl.DataFrame, year: int, year_context: dict, **config_overrides
) -> pl.DataFrame | None:
    """
    Builds the backtest projection for `year` and joins it against actual
    results for that season. Players the pipeline wouldn't have projected
    (not rostered, long-term reserve) are naturally excluded via the
    inner join — matching who'd actually have appeared in that year's
    real cheat sheet. config_overrides is forwarded to
    build_backtest_projection (lookback_seasons/decay/age_curves/
    age_trend_dampening).
    """
    projected = build_backtest_projection(season_summary, year, year_context, **config_overrides)

    actual = (
        season_summary.filter(pl.col("season") == year)
        .select(["player_id", "position", "fantasy_points_total"])
        .rename({"fantasy_points_total": "actual_points"})
    )
    if actual.height == 0:
        return None

    actual = actual.with_columns(
        pl.col("actual_points")
        .rank(method="ordinal", descending=True)
        .over("position")
        .cast(pl.Int64)
        .alias("actual_position_rank")
    )

    joined = projected.join(actual, on=["player_id", "position"], how="inner")
    joined = joined.with_columns(pl.lit(year, dtype=pl.Int64).alias("backtest_year"))
    return joined


def compute_metrics(joined: pl.DataFrame, group_cols: list[str]) -> pl.DataFrame:
    agg = [
        pl.len().alias("n_players"),
        pl.corr("projected_fantasy_points", "actual_points").alias("point_correlation"),
        pl.corr("position_rank", "actual_position_rank").alias("rank_correlation"),
        (pl.col("projected_fantasy_points") - pl.col("actual_points")).abs().mean().alias("mae"),
        (((pl.col("projected_fantasy_points") - pl.col("actual_points")) ** 2).mean()).sqrt().alias("rmse"),
    ]
    if group_cols:
        return joined.group_by(group_cols).agg(agg).sort(group_cols)
    return joined.select(agg)


def top_n_hit_rate(joined: pl.DataFrame, n: int = 12) -> pl.DataFrame:
    """
    Of the players projected as a top-N finisher at their position, what
    fraction actually finished top N that season? A concrete,
    draft-relevant check on top of the correlation/error metrics above —
    those can look fine in aggregate while still whiffing on exactly the
    picks that matter most (the players drafted early).
    """
    predicted_top = joined.filter(pl.col("position_rank") <= n)
    hits = predicted_top.filter(pl.col("actual_position_rank") <= n)

    predicted_counts = predicted_top.group_by(["backtest_year", "position"]).agg(
        pl.len().alias("predicted_top_n")
    )
    hit_counts = hits.group_by(["backtest_year", "position"]).agg(pl.len().alias("hits"))

    return (
        predicted_counts.join(hit_counts, on=["backtest_year", "position"], how="left")
        .with_columns(pl.col("hits").fill_null(0))
        .with_columns((pl.col("hits") / pl.col("predicted_top_n")).alias("hit_rate"))
        .sort(["backtest_year", "position"])
    )


def main(years: list[int]):
    season_summary = load_season_summary()

    all_results = []
    for year in years:
        print(f"\n=== Backtesting {year} ===")
        print(f"  Loading roster/age/status context for {year}...")
        year_context = load_year_context(year)
        print(f"  Building projections as of {year} (using only data before it)...")
        result = score_year(season_summary, year, year_context)
        if result is None:
            print(f"  No actual {year} results found in fantasy_points_by_season.csv — skipping.")
        else:
            print(f"  Scored {result.height} players with both a projection and an actual {year} result.")
            all_results.append(result)

    if not all_results:
        print("\nNo backtest years produced results — nothing to report.")
        return

    combined = pl.concat(all_results, how="diagonal_relaxed")

    by_year_position = compute_metrics(combined, ["backtest_year", "position"])
    by_position = compute_metrics(combined, ["position"])
    overall = compute_metrics(combined, [])
    hit_rates = top_n_hit_rate(combined, n=12)

    print("\n" + "=" * 78)
    print("ACCURACY BY YEAR AND POSITION")
    print("(point_correlation/rank_correlation closer to 1.0 = better;")
    print(" mae/rmse in fantasy points, lower = better)")
    print("=" * 78)
    with pl.Config(tbl_rows=200):
        print(by_year_position)

    print("\n" + "=" * 78)
    print("ACCURACY BY POSITION (all backtest years combined)")
    print("=" * 78)
    print(by_position)

    print("\n" + "=" * 78)
    print("OVERALL ACCURACY (all years, all positions)")
    print("=" * 78)
    print(overall)

    print("\n" + "=" * 78)
    print("TOP-12 HIT RATE")
    print("(of players projected as a top-12 finisher at their position,")
    print(" what fraction actually finished top 12 that season?)")
    print("=" * 78)
    with pl.Config(tbl_rows=200):
        print(hit_rates)

    out_path = Path("lib/backtest_results.csv")
    by_year_position.write_csv(out_path)
    print(f"\nWrote year-by-position results to {out_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backtest the projection model against past seasons.")
    parser.add_argument(
        "--years",
        type=str,
        default=None,
        help=(
            "Comma-separated seasons to backtest, e.g. 2021,2022,2023,2024,2025. "
            "Defaults to the last 5 completed seasons."
        ),
    )
    args = parser.parse_args()

    if args.years:
        selected_years = [int(y.strip()) for y in args.years.split(",")]
    else:
        last_completed = CURRENT_SEASON_OVERRIDE - 1
        selected_years = list(range(last_completed - 4, last_completed + 1))

    main(selected_years)
