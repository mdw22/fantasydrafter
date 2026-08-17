"""
grid_search.py

Searches the projection model's tunable knobs (LOOKBACK_SEASONS, DECAY,
AGE_TREND_DAMPENING, and per-position AGE_CURVES) against
bin/backtest.py's accuracy metrics, then reports the best combination
found. Does NOT touch tiering/VBD knobs (TIER_GAP_PCT_THRESHOLD etc.) or
EXCLUDED_ROSTER_STATUSES/MIN_RES_STREAK_WEEKS — those aren't predictions
with ground truth to optimize against (VBD/tiers are draft-strategy
overlays; the roster-status knobs were calibrated against specific known
cases — see PROJECTNOTES.md — and searching them against this metric
would just reward excluding hard-to-predict players, not genuine
accuracy).

Objective: mean rank_correlation across every (backtest year, position)
cell, unweighted — i.e. WR (the most players) doesn't dominate the score
just by sample size, and getting the draft ORDER right matters more here
than raw point totals (that's what the model is actually used for).

Network-dependent data (ages, rosters, roster status) for each backtest
year is fetched ONCE via bin.backtest.load_year_context() and reused
across every grid point — only the config-dependent computation
(recency_weighted_projection, age adjustment) reruns per candidate.

Search is staged, not one full joint grid (LOOKBACK_SEASONS x DECAY x
AGE_TREND_DAMPENING x 4 positions x 2 curve params is combinatorially
infeasible to search jointly):
  1. LOOKBACK_SEASONS x DECAY, jointly (most fundamental — how much
     history, how heavily weighted — and they interact with each other).
  2. AGE_TREND_DAMPENING, holding the winners from step 1.
  3. Each position's AGE_CURVES (peak_age, decline_per_year)
     independently, holding steps 1-2's winners — independently valid
     since a position's curve only affects that position's own players,
     not a simplification that loses anything.

Caveat: this optimizes against the same 2021-2025 window bin/backtest.py
uses by default. With ~5 years of data there's a real risk of tuning to
noise in this particular window rather than a genuine general
improvement — worth rerunning this backtest each future season to see
if the chosen values keep holding up.

Usage:
    python -m bin.grid_search
    python -m bin.grid_search --years 2021,2022,2023,2024,2025
"""

import argparse
import itertools

import polars as pl

from bin.backtest import load_year_context, score_year
from bin.projection_model import load_season_summary
from lib.config import (
    CURRENT_SEASON_OVERRIDE,
    LOOKBACK_SEASONS,
    DECAY,
    AGE_CURVES,
    AGE_TREND_DAMPENING,
)


def evaluate(season_summary, year_contexts: dict, years: list[int], **overrides) -> pl.DataFrame | None:
    """Runs score_year for every year with the given config overrides, returns the combined joined frame (or None if nothing scored)."""
    overrides.setdefault("verbose", False)
    results = []
    for year in years:
        result = score_year(season_summary, year, year_contexts[year], **overrides)
        if result is not None:
            results.append(result)
    if not results:
        return None
    return pl.concat(results, how="diagonal_relaxed")


def mean_rank_correlation(joined: pl.DataFrame, position: str | None = None) -> float:
    """Unweighted mean of rank_correlation across every (year, position) cell (or just one position's cells if given)."""
    df = joined if position is None else joined.filter(pl.col("position") == position)
    per_cell = (
        df.group_by(["backtest_year", "position"])
        .agg(pl.corr("position_rank", "actual_position_rank").alias("rank_correlation"))
    )
    per_cell = per_cell.filter(pl.col("rank_correlation").is_not_null())
    if per_cell.height == 0:
        return float("-inf")
    return per_cell["rank_correlation"].mean()


def search_lookback_and_decay(season_summary, year_contexts, years) -> tuple[int, float, float]:
    lookback_grid = [3, 4, 5, 6, 7, 8]
    decay_grid = [0.4, 0.5, 0.55, 0.6, 0.65, 0.7, 0.75, 0.8, 0.85]

    best = (LOOKBACK_SEASONS, DECAY, float("-inf"))
    print(f"\n--- Stage 1: LOOKBACK_SEASONS x DECAY ({len(lookback_grid)}x{len(decay_grid)} = {len(lookback_grid) * len(decay_grid)} combos) ---")
    for lookback, decay in itertools.product(lookback_grid, decay_grid):
        joined = evaluate(season_summary, year_contexts, years, lookback_seasons=lookback, decay=decay)
        if joined is None:
            continue
        score = mean_rank_correlation(joined)
        if score > best[2]:
            best = (lookback, decay, score)
            print(f"  new best: lookback_seasons={lookback} decay={decay} -> mean_rank_correlation={score:.4f}")

    print(f"  Stage 1 winner: LOOKBACK_SEASONS={best[0]} DECAY={best[1]} (mean_rank_correlation={best[2]:.4f})")
    return best


def search_age_trend_dampening(season_summary, year_contexts, years, lookback, decay) -> tuple[float, float]:
    dampening_grid = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]

    best = (AGE_TREND_DAMPENING, float("-inf"))
    print(f"\n--- Stage 2: AGE_TREND_DAMPENING ({len(dampening_grid)} values) ---")
    for dampening in dampening_grid:
        joined = evaluate(
            season_summary, year_contexts, years,
            lookback_seasons=lookback, decay=decay, age_trend_dampening=dampening,
        )
        if joined is None:
            continue
        score = mean_rank_correlation(joined)
        if score > best[1]:
            best = (dampening, score)
            print(f"  new best: age_trend_dampening={dampening} -> mean_rank_correlation={score:.4f}")

    print(f"  Stage 2 winner: AGE_TREND_DAMPENING={best[0]} (mean_rank_correlation={best[1]:.4f})")
    return best


def search_age_curves(season_summary, year_contexts, years, lookback, decay, dampening) -> dict:
    working_curves = dict(AGE_CURVES)

    print("\n--- Stage 3: per-position AGE_CURVES (peak_age, decline_per_year) ---")
    for position in ["QB", "RB", "WR", "TE"]:
        default_peak, default_decline = AGE_CURVES[position]
        peak_grid = [default_peak - 4, default_peak - 2, default_peak, default_peak + 2, default_peak + 4]
        decline_grid = [0.0, default_decline * 0.5, default_decline, default_decline * 1.5, default_decline * 2.0]

        best = (default_peak, default_decline, float("-inf"))
        for peak, decline in itertools.product(peak_grid, decline_grid):
            candidate_curves = dict(working_curves)
            candidate_curves[position] = (peak, decline)
            joined = evaluate(
                season_summary, year_contexts, years,
                lookback_seasons=lookback, decay=decay,
                age_curves=candidate_curves, age_trend_dampening=dampening,
            )
            if joined is None:
                continue
            score = mean_rank_correlation(joined, position=position)
            if score > best[2]:
                best = (peak, decline, score)

        working_curves[position] = (best[0], best[1])
        print(f"  {position}: peak_age={best[0]} decline_per_year={best[1]:.3f} (mean_rank_correlation={best[2]:.4f}, was {default_peak}/{default_decline:.3f})")

    return working_curves


def main(years: list[int]):
    season_summary = load_season_summary()

    print(f"Loading year context (ages/rosters/roster-status) for {years}...")
    year_contexts = {year: load_year_context(year) for year in years}

    baseline = evaluate(season_summary, year_contexts, years)
    baseline_score = mean_rank_correlation(baseline) if baseline is not None else float("-inf")
    print(f"\nBaseline (current config.py values): mean_rank_correlation={baseline_score:.4f}")

    best_lookback, best_decay, _ = search_lookback_and_decay(season_summary, year_contexts, years)
    best_dampening, _ = search_age_trend_dampening(season_summary, year_contexts, years, best_lookback, best_decay)
    best_curves = search_age_curves(season_summary, year_contexts, years, best_lookback, best_decay, best_dampening)

    final = evaluate(
        season_summary, year_contexts, years,
        lookback_seasons=best_lookback, decay=best_decay,
        age_curves=best_curves, age_trend_dampening=best_dampening,
    )
    final_score = mean_rank_correlation(final) if final is not None else float("-inf")

    print("\n" + "=" * 78)
    print("RESULT")
    print("=" * 78)
    print(f"Baseline mean_rank_correlation:  {baseline_score:.4f}")
    print(f"Tuned    mean_rank_correlation:  {final_score:.4f}  ({'+' if final_score >= baseline_score else ''}{final_score - baseline_score:.4f})")
    print()
    print(f"LOOKBACK_SEASONS = {best_lookback}   (was {LOOKBACK_SEASONS})")
    print(f"DECAY = {best_decay}   (was {DECAY})")
    print(f"AGE_TREND_DAMPENING = {best_dampening}   (was {AGE_TREND_DAMPENING})")
    print("AGE_CURVES = {")
    for pos in ["RB", "WR", "TE", "QB"]:
        peak, decline = best_curves[pos]
        old_peak, old_decline = AGE_CURVES[pos]
        print(f'    "{pos}": ({peak}, {decline:.3f}),   # was ({old_peak}, {old_decline:.3f})')
    print("}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Grid search the projection model's tunable knobs against backtest accuracy.")
    parser.add_argument(
        "--years",
        type=str,
        default=None,
        help="Comma-separated seasons to search against, e.g. 2021,2022,2023,2024,2025. Defaults to the last 5 completed seasons.",
    )
    args = parser.parse_args()

    if args.years:
        selected_years = [int(y.strip()) for y in args.years.split(",")]
    else:
        last_completed = CURRENT_SEASON_OVERRIDE - 1
        selected_years = list(range(last_completed - 4, last_completed + 1))

    main(selected_years)
