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

from lib.config import (
    CURRENT_SEASON_OVERRIDE,
    LOOKBACK_SEASONS,
    DECAY,
    MAX_GAMES,
    evidence_based_age_adjustment,
    EXCLUDED_ROSTER_STATUSES,
    MIN_RES_STREAK_WEEKS,
)


def load_season_summary() -> pl.DataFrame:
    path = Path("lib/fantasy_points_by_season.csv")
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


def load_currently_rostered_ids(current_season: int) -> set | None:
    """
    Pull the set of player IDs currently on an NFL team roster for
    current_season. Used to filter the projection pool down to players
    who are actually draftable — the historical stats pull has no
    awareness of retirement, free agency, or being out of the league, so
    without this filter the cheat sheet includes players who aren't
    rosterable at all (e.g. an unsigned free agent, or someone who
    retired years ago but still has recent-enough stats to rank).

    NOTE: uses load_rosters() (season-level), not load_rosters_weekly()
    (which we already use elsewhere for in-season active/reserve status).
    Column name detected dynamically (same "gsis" search pattern used
    elsewhere in this file) since we've hit real schema mismatches on
    nflreadpy tables more than once in this project — confirm locally if
    this raises a ValueError.

    Caveat: pulled during preseason, this reflects whatever roster moves
    have happened so far — it can shift (signings, cuts) right up to your
    actual draft, so it's worth rerunning close to draft day.
    """
    rosters = nfl.load_rosters(seasons=[current_season])

    if rosters.height == 0:
        print(
            f"Warning: no roster data found for {current_season} — "
            "skipping the currently-rostered filter entirely."
        )
        return None

    id_candidates = [c for c in rosters.columns if "gsis" in c.lower()]
    if not id_candidates:
        raise ValueError(
            "Could not find a gsis-style ID column in load_rosters() output. "
            f"Actual columns: {rosters.columns}"
        )
    id_col = id_candidates[0]

    return set(rosters[id_col].drop_nulls().to_list())


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
                # Needed for evidence-based age adjustment: how does the
                # player's most recent season compare to their own peak
                # within the lookback window (not the weighted average).
                pl.col("fantasy_points_per_game")
                .filter(pl.col("seasons_ago") == pl.col("seasons_ago").min())
                .first()
                .alias("most_recent_ppg"),
                pl.col("fantasy_points_per_game").max().alias("peak_ppg_in_lookback"),
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
        evidence_based_age_adjustment(pos, age, recent_ppg, peak_ppg)
        for pos, age, recent_ppg, peak_ppg in zip(
            projections["position"],
            projections["age_at_season"],
            projections["most_recent_ppg"],
            projections["peak_ppg_in_lookback"],
        )
    ]
    projections = projections.with_columns(pl.Series("age_adjustment_factor", factors))

    projections = projections.with_columns(
        (pl.col("projected_fantasy_points_raw") * pl.col("age_adjustment_factor")).alias(
            "projected_fantasy_points"
        )
    )
    return projections


def load_current_roster_status(current_season: int) -> pl.DataFrame:
    """
    Pull each player's most recent weekly roster status (e.g. "ACT" =
    active, "RES" = reserve/injured) from load_rosters_weekly(), plus how
    many consecutive most-recent weeks they've held that status. See the
    EXCLUDED_ROSTER_STATUSES comment in config.py for why this replaces
    an earlier attempt using load_injuries() — that table doesn't capture
    players who are actually on injured reserve.

    The streak length matters: a player's LAST recorded status of a season
    is often just whatever they were doing in the season's final week(s)
    (rest, load management, a minor tweak) rather than a real season-
    changing absence. Only a status held for MIN_RES_STREAK_WEEKS+
    consecutive weeks is treated as significant — see that constant in
    config.py for the real-world cases (Tyreek Hill vs. Rashee Rice) this
    was calibrated against.

    NOTE: the ID column name here is detected dynamically (searching for
    a column containing "gsis") rather than hardcoded, since we've hit
    schema-name mismatches with nflreadpy tables more than once in this
    project already. Confirm locally if this raises a ValueError.

    NOTE: load_rosters_weekly() validates its `seasons` argument against
    nflreadpy's own nfl.get_current_season() (season-in-progress logic,
    which doesn't roll over to the new season until after Labor Day) —
    NOT CURRENT_SEASON_OVERRIDE (the season we're drafting for, which
    rolls over as soon as roster-building starts each March). Requesting
    CURRENT_SEASON_OVERRIDE here during the pre-season window raises a
    ValueError since no weekly data exists yet for a season that hasn't
    started. Clamp against nflreadpy's own bound so this self-corrects
    once the season actually kicks off (nfl.get_current_season() then
    matches CURRENT_SEASON_OVERRIDE and weekly data exists).
    """
    max_available_season = nfl.get_current_season()
    seasons_to_load = sorted(
        {min(current_season, max_available_season), min(current_season - 1, max_available_season)}
    )
    rosters = nfl.load_rosters_weekly(seasons=seasons_to_load)

    if rosters.height == 0:
        return pl.DataFrame(
            schema={
                "player_id": pl.Utf8,
                "roster_status": pl.Utf8,
                "roster_status_streak_weeks": pl.Int64,
            }
        )

    id_candidates = [c for c in rosters.columns if "gsis" in c.lower()]
    if not id_candidates:
        raise ValueError(
            "Could not find a gsis-style ID column in load_rosters_weekly() "
            f"output. Actual columns: {rosters.columns}. Update id_col below "
            "manually once you know the right one."
        )
    id_col = id_candidates[0]

    rosters_sorted = rosters.sort(["season", "week"], descending=[True, True])

    # Tag each row with a per-player "run id" that increments every time
    # status changes, walking backward from the most recent week — so the
    # most-recent run (run_id == 1) is exactly the player's current
    # trailing streak on their latest status.
    rosters_sorted = rosters_sorted.with_columns(
        (pl.col("status") != pl.col("status").shift(1).over(id_col))
        .fill_null(True)
        .alias("status_changed")
    )
    rosters_sorted = rosters_sorted.with_columns(
        pl.col("status_changed").cum_sum().over(id_col).alias("run_id")
    )

    latest = (
        rosters_sorted.filter(pl.col("run_id") == 1)
        .group_by(id_col, maintain_order=True)
        .agg(
            [
                pl.col("status").first().alias("roster_status"),
                pl.len().alias("roster_status_streak_weeks"),
            ]
        )
        .rename({id_col: "player_id"})
    )
    return latest


def filter_excluded_roster_status(
    projections: pl.DataFrame, roster_status: pl.DataFrame
) -> pl.DataFrame:
    """
    Drop players whose most recent roster status is in
    EXCLUDED_ROSTER_STATUSES (e.g. "RES" — season-ending injury) AND who
    have held that status for MIN_RES_STREAK_WEEKS+ consecutive weeks, from
    the player pool entirely. See the EXCLUDED_ROSTER_STATUSES and
    MIN_RES_STREAK_WEEKS comments in config.py for why this is a hard drop
    rather than a discount, and why streak length (not just the single most
    recent week) gates it.
    """
    projections = projections.join(roster_status, on="player_id", how="left")

    before_count = projections.height
    # fill_null(False): a player with no matching row in roster_status
    # (e.g. no weekly-roster data yet, ID didn't match) must NOT be treated
    # as excluded — is_in() on a null status evaluates to null, and
    # .filter() drops null rows same as False, so without this every
    # unmatched player would silently disappear from the pool.
    excluded_mask = (
        pl.col("roster_status").is_in(list(EXCLUDED_ROSTER_STATUSES))
        & (pl.col("roster_status_streak_weeks") >= MIN_RES_STREAK_WEEKS)
    ).fill_null(False)
    projections = projections.filter(~excluded_mask)
    dropped = before_count - projections.height
    if dropped:
        print(f"  Dropped {dropped} players with {MIN_RES_STREAK_WEEKS}+ consecutive weeks on an excluded roster status ({sorted(EXCLUDED_ROSTER_STATUSES)}).")
    return projections


def add_position_rank(projections: pl.DataFrame) -> pl.DataFrame:
    return projections.with_columns(
        pl.col("projected_fantasy_points")
        .rank(method="ordinal", descending=True)
        .over("position")
        .cast(pl.Int64)  # rank() returns an unsigned type; cast so later
        # subtraction (adp_proxy_rank - position_rank) can go negative
        # instead of wrapping around to a huge unsigned value.
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
        .cast(pl.Int64)  # same overflow reason as position_rank above
        .alias("adp_proxy_rank")
    )

    # Also compute an OVERALL (cross-position) proxy rank — real draft
    # position isn't scoped to a single position, so comparing VBD rank
    # (which is also cross-position) against a position-scoped proxy isn't
    # meaningful. This ranks all skill players together by prior-season
    # total points, same as real ADP would span positions.
    prior = prior.with_columns(
        pl.col("fantasy_points_total")
        .rank(method="ordinal", descending=True)
        .cast(pl.Int64)
        .alias("adp_proxy_rank_overall")
    )

    prior = prior.select(["player_id", "adp_proxy_rank", "adp_proxy_rank_overall"])

    projections = projections.join(prior, on="player_id", how="left")
    projections = projections.with_columns(
        (pl.col("adp_proxy_rank") - pl.col("position_rank")).alias("value_vs_adp_proxy")
    )
    return projections


def main(force_recompute: bool = False):
    out_path = Path("lib/projections.csv")

    if out_path.exists() and not force_recompute:
        print(f"{out_path} already exists — skipping recompute. (pass --force to redo)")
        result = pl.read_csv(out_path)
    else:
        current_season = CURRENT_SEASON_OVERRIDE

        print("Loading season summary from compute_fantasy_points.py output...")
        season_summary = load_season_summary()

        print("Loading player birth dates for age adjustment...")
        ages = load_player_ages(current_season)

        print(f"Building recency-weighted projections ({LOOKBACK_SEASONS}-season lookback, decay={DECAY})...")
        projections = recency_weighted_projection(season_summary, current_season)

        print("Filtering to currently-rostered players (excludes free agents/retirees)...")
        rostered_ids = load_currently_rostered_ids(current_season)
        if rostered_ids is not None:
            before_count = projections.height
            projections = projections.filter(pl.col("player_id").is_in(list(rostered_ids)))
            print(f"  Kept {projections.height} of {before_count} players (dropped {before_count - projections.height} not on a current roster).")

        print("Applying position-specific age adjustment (evidence-based)...")
        projections = apply_age_adjustment(projections, ages)

        print("Loading current roster status (active/reserve)...")
        roster_status = load_current_roster_status(current_season)

        print("Dropping players with an excluded roster status (e.g. season-ending injury)...")
        projections = filter_excluded_roster_status(projections, roster_status)

        print("Ranking within position...")
        projections = add_position_rank(projections)

        print("Computing ADP proxy from prior-season finish...")
        projections = add_adp_proxy(projections, season_summary, current_season)

        result = projections.sort(["position", "position_rank"])
        result.write_csv(out_path)
        print(f"Done. Wrote {result.height} player projections to {out_path}")

    print("\nTop 5 projected players per position:")
    with pl.Config(tbl_rows=5):
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
                        "age_adjustment_factor",
                        "roster_status",
                        "position_rank",
                        "adp_proxy_rank",
                        "value_vs_adp_proxy",
                    ]
                )
            )


if __name__ == "__main__":
    import sys

    main(force_recompute="--force" in sys.argv)