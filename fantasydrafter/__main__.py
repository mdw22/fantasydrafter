"""
projection_model.py

Step 2 of the fantasy draft tool: build forward-looking Full PPR fantasy
point projections from the historical data computed in
compute_fantasy_points.py, plus an ADP proxy (since nflreadpy has no true
ADP data).

NOTE: Not run/tested against live nflreadpy data (no network access in the
environment this was written in). Run locally and sanity-check the output
before trusting it — see the suggested checks at the bottom of this file.

Requires config.py (same directory) for tunable projection/age-adjustment
settings.

Usage:
    python projection_model.py
    python projection_model.py --force        # ignore cached output
"""

from pathlib import Path
import datetime as dt

import nflreadpy as nfl
import polars as pl

from config import LOOKBACK_SEASONS, DECAY, MAX_GAMES, age_adjustment_factor


def load_season_summary() -> pl.DataFrame:
    path = Path("fantasy_points_by_season.csv")
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found — run compute_fantasy_points.py first."
        )
    return pl.read_csv(path)


def load_player_ages(current_season: int) -> pl.DataFrame:
    """
    Pull birth dates from nflreadpy's player info table and compute each
    player's approximate age as of September 1 of the given season (a
    reasonable proxy for "age during that season").
    """
    players = nfl.load_players()  # comprehensive player info, incl. birth_date

    # Column name per nflreadpy docs; confirm locally if this errors —
    # schemas do shift between versions.
    players = players.select(["gsis_id", "birth_date"]).rename(
        {"gsis_id": "player_id"}
    )

    # birth_date can come back as a string depending on nflreadpy/polars
    # version — parse it into a proper Date type if it isn't one already,
    # so we can do date arithmetic on it below.
    if players.schema["birth_date"] != pl.Date:
        players = players.with_columns(
            pl.col("birth_date").str.to_date(strict=False).alias("birth_date")
        )

    season_start = dt.date(current_season, 9, 1)

    def age_expr():
        return (
            (pl.lit(season_start) - pl.col("birth_date")).dt.total_days() / 365.25
        )

    players = players.with_columns(age_expr().alias("age_at_season"))
    return players.select(["player_id", "age_at_season"])


def recency_weighted_projection(season_summary: pl.DataFrame, current_season: int) -> pl.DataFrame:
    """
    For each player, take their last LOOKBACK_SEASONS seasons (seasons
    strictly before current_season), weight per-game fantasy points by
    recency (exponential decay), and estimate:
      - projected_ppg: weighted-average per-game fantasy points
      - projected_games: weighted-average games played, capped at MAX_GAMES
      - projected_fantasy_points: projected_ppg * projected_games
    """
    history = season_summary.filter(pl.col("season") < current_season)

    # Seasons-ago and decay weight per row.
    history = history.with_columns(
        (pl.lit(current_season) - pl.col("season")).alias("seasons_ago")
    )
    history = history.filter(pl.col("seasons_ago") <= LOOKBACK_SEASONS)
    history = history.with_columns(
        (pl.lit(DECAY) ** pl.col("seasons_ago")).alias("weight")
    )

    weighted = (
        history.group_by(["player_id", "player_display_name", "position"])
        .agg(
            [
                (
                    (pl.col("fantasy_points_per_game") * pl.col("weight")).sum()
                    / pl.col("weight").sum()
                ).alias("projected_ppg"),
                (
                    (pl.col("games_played") * pl.col("weight")).sum()
                    / pl.col("weight").sum()
                ).alias("avg_games_played"),
                pl.col("season").max().alias("most_recent_season"),
            ]
        )
    )

    weighted = weighted.with_columns(
        pl.col("avg_games_played").clip(upper_bound=MAX_GAMES).alias("projected_games")
    )

    weighted = weighted.with_columns(
        (pl.col("projected_ppg") * pl.col("projected_games")).alias(
            "projected_fantasy_points_raw"
        )
    )

    return weighted


def apply_age_adjustment(projections: pl.DataFrame, ages: pl.DataFrame) -> pl.DataFrame:
    projections = projections.join(ages, on="player_id", how="left")

    factors = [
        age_adjustment_factor(pos, age)
        for pos, age in zip(projections["position"], projections["age_at_season"])
    ]
    projections = projections.with_columns(pl.Series("age_adjustment_factor", factors))

    projections = projections.with_columns(
        (pl.col("projected_fantasy_points_raw") * pl.col("age_adjustment_factor")).alias(
            "projected_fantasy_points"
        )
    )
    return projections


def add_position_rank(projections: pl.DataFrame) -> pl.DataFrame:
    return projections.with_columns(
        pl.col("projected_fantasy_points")
        .rank(method="ordinal", descending=True)
        .over("position")
        .alias("position_rank")
    )


def add_adp_proxy(projections: pl.DataFrame, season_summary: pl.DataFrame, current_season: int) -> pl.DataFrame:
    """
    ADP proxy: each player's rank within their position based on their
    single most recent completed season's total fantasy points. This
    approximates "market" draft capital independent of our own model,
    so (adp_proxy_rank - position_rank) surfaces potential value picks
    (positive = model likes them more than their recent finish suggests)
    or reaches (negative).
    """
    prior_season = current_season - 1
    prior = season_summary.filter(pl.col("season") == prior_season)

    prior = prior.with_columns(
        pl.col("fantasy_points_total")
        .rank(method="ordinal", descending=True)
        .over("position")
        .alias("adp_proxy_rank")
    )

    prior = prior.select(["player_id", "adp_proxy_rank"])

    projections = projections.join(prior, on="player_id", how="left")
    projections = projections.with_columns(
        (pl.col("adp_proxy_rank") - pl.col("position_rank")).alias("value_vs_adp_proxy")
    )
    return projections


def main(force_recompute: bool = False):
    out_path = Path("projections.csv")

    if out_path.exists() and not force_recompute:
        print(f"{out_path} already exists — skipping recompute. (pass --force to redo)")
        result = pl.read_csv(out_path)
    else:
        current_season = nfl.get_current_season()

        print("Loading season summary from compute_fantasy_points.py output...")
        season_summary = load_season_summary()

        print("Loading player birth dates for age adjustment...")
        ages = load_player_ages(current_season)

        print(f"Building recency-weighted projections ({LOOKBACK_SEASONS}-season lookback, decay={DECAY})...")
        projections = recency_weighted_projection(season_summary, current_season)

        print("Applying position-specific age adjustment...")
        projections = apply_age_adjustment(projections, ages)

        print("Ranking within position...")
        projections = add_position_rank(projections)

        print("Computing ADP proxy from prior-season finish...")
        projections = add_adp_proxy(projections, season_summary, current_season)

        result = projections.sort(["position", "position_rank"])
        result.write_csv(out_path)
        print(f"Done. Wrote {result.height} player projections to {out_path}")

    print("\nTop 5 projected players per position:")
    for pos in ["QB", "RB", "WR", "TE"]:
        print(f"\n-- {pos} --")
        print(
            result.filter(pl.col("position") == pos)
            .sort("position_rank")
            .head(5)
            .select(
                [
                    "player_display_name",
                    "projected_fantasy_points",
                    "position_rank",
                    "adp_proxy_rank",
                    "value_vs_adp_proxy",
                ]
            )
        )


if __name__ == "__main__":
    import sys

    main(force_recompute="--force" in sys.argv)