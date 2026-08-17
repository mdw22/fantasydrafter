"""
grid_search.py

Searches the projection model's tunable knobs (LOOKBACK_SEASONS, DECAY,
AGE_TREND_DAMPENING, per-position AGE_CURVES, TEAM_OFFENSE_ADJUSTMENT_
STRENGTH, QB_RUSHING_YARDS_WEIGHT/QB_RUSHING_EMPHASIS_STRENGTH, and
TARGET_SHARE_ADJUSTMENT_STRENGTH) against bin/backtest.py's accuracy
metrics, then reports the best combination found. Does NOT touch
tiering/VBD knobs (TIER_GAP_PCT_THRESHOLD etc.) or
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

Network-dependent data (ages, rosters, roster status, team offense
strength, target share) for each backtest year is fetched ONCE via
bin.backtest.load_year_context() and reused across every grid point —
only the config-dependent computation (recency_weighted_projection, age
adjustment, team offense/QB-rushing/target-share adjustments) reruns per
candidate.

Search is staged, not one full joint grid (combinatorially infeasible
to search everything jointly):
  1. LOOKBACK_SEASONS x DECAY, jointly (most fundamental — how much
     history, how heavily weighted — and they interact with each other).
  2. AGE_TREND_DAMPENING, holding the winners from step 1.
  3. Each position's AGE_CURVES (peak_age, decline_per_year)
     independently, holding steps 1-2's winners — independently valid
     since a position's curve only affects that position's own players,
     not a simplification that loses anything.
  4. TEAM_OFFENSE_ADJUSTMENT_STRENGTH, holding steps 1-3's winners.
  5. QB_RUSHING_YARDS_WEIGHT x QB_RUSHING_EMPHASIS_STRENGTH, jointly
     (weight determines what the signal even IS, strength determines how
     much it moves points — searched together since they interact).
  6. TARGET_SHARE_ADJUSTMENT_STRENGTH, holding everything above.

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
    TEAM_OFFENSE_ADJUSTMENT_STRENGTH,
    QB_RUSHING_YARDS_WEIGHT,
    QB_RUSHING_EMPHASIS_STRENGTH,
    TARGET_SHARE_ADJUSTMENT_STRENGTH,
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
    """
    Fixed, ABSOLUTE search grids for peak_age/decline_per_year — NOT
    centered on whatever's currently in config.py. Centering on the
    current value lets the window walk arbitrarily far from a sane
    range across repeated runs (found empirically: RB peak_age drifted
    27 -> 23 -> 19 over two successive searches, each one re-centering
    on the last run's already-shifted result, with no sign of
    converging) — that's a search-methodology artifact, not a real
    signal. A fixed grid means every run searches the same space
    regardless of what's currently applied.
    """
    working_curves = dict(AGE_CURVES)
    peak_grid = [21, 23, 25, 27, 29, 31, 33]
    decline_grid = [0.0, 0.02, 0.04, 0.06, 0.08, 0.10]

    print("\n--- Stage 3: per-position AGE_CURVES (peak_age, decline_per_year) ---")
    for position in ["QB", "RB", "WR", "TE"]:
        default_peak, default_decline = AGE_CURVES[position]

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


def search_team_offense_strength(season_summary, year_contexts, years, lookback, decay, dampening, curves) -> tuple[float, float]:
    strength_grid = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0, 1.2, 1.5]

    best = (TEAM_OFFENSE_ADJUSTMENT_STRENGTH, float("-inf"))
    print(f"\n--- Stage 4: TEAM_OFFENSE_ADJUSTMENT_STRENGTH ({len(strength_grid)} values) ---")
    for strength in strength_grid:
        joined = evaluate(
            season_summary, year_contexts, years,
            lookback_seasons=lookback, decay=decay,
            age_curves=curves, age_trend_dampening=dampening,
            team_offense_adjustment_strength=strength,
        )
        if joined is None:
            continue
        score = mean_rank_correlation(joined)
        if score > best[1]:
            best = (strength, score)
            print(f"  new best: team_offense_adjustment_strength={strength} -> mean_rank_correlation={score:.4f}")

    print(f"  Stage 4 winner: TEAM_OFFENSE_ADJUSTMENT_STRENGTH={best[0]} (mean_rank_correlation={best[1]:.4f})")
    return best


def search_qb_rushing_emphasis(season_summary, year_contexts, years, lookback, decay, dampening, curves, team_strength) -> tuple[float, float, float]:
    weight_grid = [1.0, 1.5, 2.0, 2.5, 3.0, 4.0]
    strength_grid = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6]

    best = (QB_RUSHING_YARDS_WEIGHT, QB_RUSHING_EMPHASIS_STRENGTH, float("-inf"))
    print(f"\n--- Stage 5: QB_RUSHING_YARDS_WEIGHT x QB_RUSHING_EMPHASIS_STRENGTH ({len(weight_grid)}x{len(strength_grid)} = {len(weight_grid) * len(strength_grid)} combos) ---")
    for weight, strength in itertools.product(weight_grid, strength_grid):
        joined = evaluate(
            season_summary, year_contexts, years,
            lookback_seasons=lookback, decay=decay,
            age_curves=curves, age_trend_dampening=dampening,
            team_offense_adjustment_strength=team_strength,
            qb_rushing_yards_weight=weight, qb_rushing_emphasis_strength=strength,
        )
        if joined is None:
            continue
        score = mean_rank_correlation(joined, position="QB")
        if score > best[2]:
            best = (weight, strength, score)
            print(f"  new best: qb_rushing_yards_weight={weight} qb_rushing_emphasis_strength={strength} -> mean_rank_correlation(QB)={score:.4f}")

    print(f"  Stage 5 winner: QB_RUSHING_YARDS_WEIGHT={best[0]} QB_RUSHING_EMPHASIS_STRENGTH={best[1]} (mean_rank_correlation(QB)={best[2]:.4f})")
    return best


def search_target_share_strength(season_summary, year_contexts, years, lookback, decay, dampening, curves, team_strength, qb_weight, qb_strength) -> tuple[float, float]:
    strength_grid = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0, 1.2, 1.5]

    best = (TARGET_SHARE_ADJUSTMENT_STRENGTH, float("-inf"))
    print(f"\n--- Stage 6: TARGET_SHARE_ADJUSTMENT_STRENGTH ({len(strength_grid)} values) ---")
    for strength in strength_grid:
        joined = evaluate(
            season_summary, year_contexts, years,
            lookback_seasons=lookback, decay=decay,
            age_curves=curves, age_trend_dampening=dampening,
            team_offense_adjustment_strength=team_strength,
            qb_rushing_yards_weight=qb_weight, qb_rushing_emphasis_strength=qb_strength,
            target_share_adjustment_strength=strength,
        )
        if joined is None:
            continue
        # RB/WR/TE only — target share doesn't touch QB.
        score = (
            mean_rank_correlation(joined, position="RB")
            + mean_rank_correlation(joined, position="WR")
            + mean_rank_correlation(joined, position="TE")
        ) / 3
        if score > best[1]:
            best = (strength, score)
            print(f"  new best: target_share_adjustment_strength={strength} -> mean_rank_correlation(RB/WR/TE)={score:.4f}")

    print(f"  Stage 6 winner: TARGET_SHARE_ADJUSTMENT_STRENGTH={best[0]} (mean_rank_correlation(RB/WR/TE)={best[1]:.4f})")
    return best


def main(years: list[int]):
    season_summary = load_season_summary()

    print(f"Loading year context (ages/rosters/roster-status) for {years}...")
    year_contexts = {year: load_year_context(season_summary, year) for year in years}

    baseline = evaluate(season_summary, year_contexts, years)
    baseline_score = mean_rank_correlation(baseline) if baseline is not None else float("-inf")
    print(f"\nBaseline (current config.py values): mean_rank_correlation={baseline_score:.4f}")

    best_lookback, best_decay, _ = search_lookback_and_decay(season_summary, year_contexts, years)
    best_dampening, _ = search_age_trend_dampening(season_summary, year_contexts, years, best_lookback, best_decay)
    best_curves = search_age_curves(season_summary, year_contexts, years, best_lookback, best_decay, best_dampening)
    best_team_strength, _ = search_team_offense_strength(
        season_summary, year_contexts, years, best_lookback, best_decay, best_dampening, best_curves
    )
    best_qb_weight, best_qb_strength, _ = search_qb_rushing_emphasis(
        season_summary, year_contexts, years, best_lookback, best_decay, best_dampening, best_curves, best_team_strength
    )
    best_target_share_strength, _ = search_target_share_strength(
        season_summary, year_contexts, years, best_lookback, best_decay, best_dampening, best_curves,
        best_team_strength, best_qb_weight, best_qb_strength,
    )

    final = evaluate(
        season_summary, year_contexts, years,
        lookback_seasons=best_lookback, decay=best_decay,
        age_curves=best_curves, age_trend_dampening=best_dampening,
        team_offense_adjustment_strength=best_team_strength,
        qb_rushing_yards_weight=best_qb_weight, qb_rushing_emphasis_strength=best_qb_strength,
        target_share_adjustment_strength=best_target_share_strength,
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
    print(f"TEAM_OFFENSE_ADJUSTMENT_STRENGTH = {best_team_strength}   (was {TEAM_OFFENSE_ADJUSTMENT_STRENGTH})")
    print(f"QB_RUSHING_YARDS_WEIGHT = {best_qb_weight}   (was {QB_RUSHING_YARDS_WEIGHT})")
    print(f"QB_RUSHING_EMPHASIS_STRENGTH = {best_qb_strength}   (was {QB_RUSHING_EMPHASIS_STRENGTH})")
    print(f"TARGET_SHARE_ADJUSTMENT_STRENGTH = {best_target_share_strength}   (was {TARGET_SHARE_ADJUSTMENT_STRENGTH})")


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
