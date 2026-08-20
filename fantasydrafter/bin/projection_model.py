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
    AGE_CURVES,
    AGE_TREND_DAMPENING,
    TEAM_OFFENSE_ADJUSTMENT_STRENGTH,
    QB_RUSHING_EMPHASIS_STRENGTH,
    TARGET_SHARE_ADJUSTMENT_STRENGTH,
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


# load_rosters() and load_team_stats() disagree on Arizona's abbreviation
# (AZ vs ARI) — found empirically (see load_player_teams), not documented
# anywhere. Normalize to team_stats' convention ("ARI") so the two join
# cleanly; without this, every Arizona player silently got no team-offense
# signal at all (fell back to the null-safe 1.0x default with no error).
TEAM_CODE_ALIASES = {"AZ": "ARI"}


def load_player_teams(current_season: int) -> pl.DataFrame | None:
    """
    Pulls each player's current team for current_season from
    load_rosters() (season-level roster snapshot). Backs both
    load_currently_rostered_ids (the draftable-player filter) and
    apply_team_offense_adjustment (joining team-level context onto
    individual player rows) — team codes normalized via
    TEAM_CODE_ALIASES so the latter join doesn't silently miss players.

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
        return None

    id_candidates = [c for c in rosters.columns if "gsis" in c.lower()]
    if not id_candidates:
        raise ValueError(
            "Could not find a gsis-style ID column in load_rosters() output. "
            f"Actual columns: {rosters.columns}"
        )
    id_col = id_candidates[0]

    return (
        rosters.select([id_col, "team"])
        .drop_nulls()
        .unique(subset=[id_col], keep="first")
        .rename({id_col: "player_id"})
        .with_columns(pl.col("team").replace(TEAM_CODE_ALIASES))
    )


def load_currently_rostered_ids(current_season: int) -> set | None:
    """
    Pull the set of player IDs currently on an NFL team roster for
    current_season, via load_player_teams(). Used to filter the
    projection pool down to players who are actually draftable — the
    historical stats pull has no awareness of retirement, free agency,
    or being out of the league, so without this filter the cheat sheet
    includes players who aren't rosterable at all (e.g. an unsigned free
    agent, or someone who retired years ago but still has recent-enough
    stats to rank).
    """
    player_teams = load_player_teams(current_season)

    if player_teams is None:
        print(
            f"Warning: no roster data found for {current_season} — "
            "skipping the currently-rostered filter entirely."
        )
        return None

    return set(player_teams["player_id"].to_list())


def recency_weighted_projection(
    season_summary: pl.DataFrame,
    current_season: int,
    lookback_seasons: int = LOOKBACK_SEASONS,
    decay: float = DECAY,
) -> pl.DataFrame:
    """
    For each player, take their last lookback_seasons seasons (seasons
    strictly before current_season), weight per-game fantasy points by
    recency (exponential decay), and estimate:
      - projected_ppg: weighted-average per-game fantasy points
      - projected_games: weighted-average games played, capped at MAX_GAMES
      - projected_fantasy_points: projected_ppg * projected_games

    lookback_seasons/decay default to module-level config but can be
    overridden — used by bin/grid_search.py to test alternate values
    without mutating global config state.
    """
    history = season_summary.filter(pl.col("season") < current_season)

    # Seasons-ago and decay weight per row.
    history = history.with_columns(
        (pl.lit(current_season) - pl.col("season")).alias("seasons_ago")
    )
    history = history.filter(pl.col("seasons_ago") <= lookback_seasons)
    history = history.with_columns(
        (pl.lit(decay) ** pl.col("seasons_ago")).alias("weight")
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


def apply_age_adjustment(
    projections: pl.DataFrame,
    ages: pl.DataFrame,
    age_curves: dict = AGE_CURVES,
    age_trend_dampening: float = AGE_TREND_DAMPENING,
) -> pl.DataFrame:
    """
    age_curves/age_trend_dampening default to module-level config but can
    be overridden — used by bin/grid_search.py.
    """
    projections = projections.join(ages, on="player_id", how="left")

    # DEF is a team, not a person — it has no age at all, and applying a
    # skill-position decline curve to it wouldn't mean anything. Forced to
    # 1.0 explicitly here rather than relying on age_at_season coming back
    # null for a team-abbreviation "player_id" (which would also produce
    # 1.0 via age_adjustment_factor's own None-handling) — this way it's a
    # deliberate exemption, not an emergent side effect of two unrelated
    # null-safety checks. K is NOT exempt from age adjustment in general,
    # but K also isn't a key in AGE_CURVES (config.py), so it already gets
    # 1.0 the same way any other uncovered position would — no separate
    # handling needed for that one.
    factors = [
        1.0
        if pos == "DEF"
        else evidence_based_age_adjustment(
            pos, age, recent_ppg, peak_ppg, age_curves, age_trend_dampening
        )
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
    projections: pl.DataFrame, roster_status: pl.DataFrame, verbose: bool = True
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
    if dropped and verbose:
        print(f"  Dropped {dropped} players with {MIN_RES_STREAK_WEEKS}+ consecutive weeks on an excluded roster status ({sorted(EXCLUDED_ROSTER_STATUSES)}).")
    return projections


def load_team_offense_strength(
    current_season: int,
    lookback_seasons: int = LOOKBACK_SEASONS,
    decay: float = DECAY,
) -> pl.DataFrame:
    """
    Each team's recency-weighted offensive yards-per-game (passing +
    rushing), relative to the league average over the same window, as of
    current_season. Mirrors recency_weighted_projection's weighting
    exactly (same seasons-ago / DECAY-power formula), just applied to
    team-level stats instead of player-level — same LOOKBACK_SEASONS/
    DECAY by default so "how much recent history matters" stays
    consistent between the two, though callers can override (used by
    bin/grid_search.py).

    Pulls seasons strictly before current_season (last_completed_season
    down through the lookback window) — mirrors compute_fantasy_points.py's
    CURRENT_SEASON_OVERRIDE handling: current_season may be the season
    we're drafting FOR (unplayed), so requesting it directly would ask
    nflreadpy for a season with no games yet.

    Uses summary_level="reg" directly (regular season only) rather than
    filtering post-hoc — same reasoning as the postseason-mixing fix in
    compute_fantasy_points.py.
    """
    last_completed_season = current_season - 1
    seasons = list(range(last_completed_season - lookback_seasons + 1, last_completed_season + 1))

    team_stats = nfl.load_team_stats(seasons=seasons, summary_level="reg")
    team_stats = team_stats.with_columns(
        ((pl.col("passing_yards") + pl.col("rushing_yards")) / pl.col("games")).alias("yards_per_game")
    )

    team_stats = team_stats.with_columns(
        (pl.lit(current_season) - pl.col("season")).alias("seasons_ago")
    )
    team_stats = team_stats.with_columns(
        (pl.lit(decay) ** pl.col("seasons_ago")).alias("weight")
    )

    weighted = team_stats.group_by("team").agg(
        (
            (pl.col("yards_per_game") * pl.col("weight")).sum() / pl.col("weight").sum()
        ).alias("team_yards_per_game")
    )

    league_avg = weighted["team_yards_per_game"].mean()
    weighted = weighted.with_columns(
        (pl.col("team_yards_per_game") / league_avg).alias("team_yards_ratio")
    )
    return weighted.select(["team", "team_yards_ratio"])


def apply_team_offense_adjustment(
    projections: pl.DataFrame,
    player_teams: pl.DataFrame | None,
    team_offense: pl.DataFrame,
    team_offense_adjustment_strength: float = TEAM_OFFENSE_ADJUSTMENT_STRENGTH,
) -> pl.DataFrame:
    """
    Multiplies projected_fantasy_points by a factor derived from the
    player's current team's offensive yards-per-game relative to league
    average (see load_team_offense_strength). strength=0 leaves everyone
    at 1.0x; strength=1 fully passes through the team's ratio. Players
    with no team match (no roster data available) get 1.0x — no team
    signal, no adjustment either way, not an exclusion.
    """
    if player_teams is None:
        return projections.with_columns(pl.lit(1.0).alias("team_offense_adjustment_factor"))

    projections = projections.join(player_teams, on="player_id", how="left")
    projections = projections.join(team_offense, on="team", how="left")

    projections = projections.with_columns(
        (
            1.0
            + team_offense_adjustment_strength * (pl.col("team_yards_ratio").fill_null(1.0) - 1.0)
        ).alias("team_offense_adjustment_factor")
    )

    projections = projections.with_columns(
        (pl.col("projected_fantasy_points") * pl.col("team_offense_adjustment_factor")).alias(
            "projected_fantasy_points"
        )
    )
    return projections


def _points_weighted_position_average(metric_col: str) -> pl.Expr:
    """
    Points-weighted average of metric_col within each position — NOT a
    simple per-player mean. A simple mean gets dragged down hard by the
    long tail of career backups every roster carries (2nd/3rd-string
    players with near-zero relevant usage), which inflated every real
    starter's ratio far beyond anything reasonable (found empirically:
    a simple mean put Josh Allen's qb_yards_ratio at 2.8x). Weighting
    each player's contribution by their own pre-adjustment
    projected_fantasy_points means irrelevant bench players barely move
    the baseline, so a real starter's ratio stays sane. Requires
    "position" and "projected_fantasy_points" columns on the frame this
    expression is used against.
    """
    weight = pl.col("projected_fantasy_points").clip(lower_bound=0)
    valid_weight = pl.when(pl.col(metric_col).is_not_null()).then(weight).otherwise(0.0)
    return (
        (pl.col(metric_col).fill_null(0.0) * valid_weight).sum().over("position")
        / valid_weight.sum().over("position")
    )


def load_qb_rushing_emphasis(
    season_summary: pl.DataFrame,
    current_season: int,
    lookback_seasons: int = LOOKBACK_SEASONS,
    decay: float = DECAY,
) -> pl.DataFrame:
    """
    Each QB's recency-weighted rushing yards, UNNORMALIZED — see
    QB_RUSHING_EMPHASIS_STRENGTH in config.py for why. Ratio-to-position-
    average is computed later in apply_qb_rushing_emphasis, against the
    currently-relevant player pool rather than this whole historical
    universe.

    REAL BUG FIXED (Goff/Barkley/Evans diagnostic — see PROJECTNOTES.md):
    this used to be "passing_yards + WEIGHT x rushing_yards", which was
    dominated by passing_yards for any high-volume passer — a pure
    pocket passer in a big-volume offense got nearly as much "rushing
    emphasis" credit as an actual dual-threat QB, double-counting yards
    already earned via passing. Rushing yards alone, with no weight
    multiplier — a constant multiplier on the only term in the ratio
    would cancel out against the (equally scaled) position-average
    baseline anyway, so there was nothing left to tune there once
    passing_yards was removed.

    Same seasons-ago / weight formula as recency_weighted_projection,
    just applied to rushing yards instead of fantasy points.
    """
    history = season_summary.filter(
        (pl.col("season") < current_season) & (pl.col("position") == "QB")
    )
    history = history.with_columns(
        (pl.lit(current_season) - pl.col("season")).alias("seasons_ago")
    )
    history = history.filter(pl.col("seasons_ago") <= lookback_seasons)
    history = history.with_columns(
        (pl.lit(decay) ** pl.col("seasons_ago")).alias("weight")
    )
    history = history.with_columns(pl.col("rushing_yards").alias("emphasized_yards"))

    weighted = history.group_by("player_id").agg(
        (
            (pl.col("emphasized_yards") * pl.col("weight")).sum() / pl.col("weight").sum()
        ).alias("weighted_emphasized_yards")
    )
    return weighted


def apply_qb_rushing_emphasis(
    projections: pl.DataFrame,
    qb_yards: pl.DataFrame,
    strength: float = QB_RUSHING_EMPHASIS_STRENGTH,
) -> pl.DataFrame:
    """
    Multiplies QB projected_fantasy_points by a factor derived from the
    QB's own emphasized-yards ratio vs. the (currently-rostered) QB
    position average — see load_qb_rushing_emphasis and
    QB_RUSHING_EMPHASIS_STRENGTH in config.py. Non-QB positions and QBs
    with no rushing/passing history (e.g. a true rookie) get 1.0x.
    """
    projections = projections.join(qb_yards, on="player_id", how="left")

    projections = projections.with_columns(
        _points_weighted_position_average("weighted_emphasized_yards").alias("qb_position_avg_emphasized_yards")
    )
    projections = projections.with_columns(
        (pl.col("weighted_emphasized_yards") / pl.col("qb_position_avg_emphasized_yards")).alias("qb_yards_ratio")
    )

    projections = projections.with_columns(
        pl.when(pl.col("position") == "QB")
        .then(1.0 + strength * (pl.col("qb_yards_ratio").fill_null(1.0) - 1.0))
        .otherwise(1.0)
        .alias("qb_rushing_emphasis_factor")
    )

    projections = projections.with_columns(
        (pl.col("projected_fantasy_points") * pl.col("qb_rushing_emphasis_factor")).alias(
            "projected_fantasy_points"
        )
    )
    return projections


def load_target_share(
    season_summary: pl.DataFrame,
    current_season: int,
    lookback_seasons: int = LOOKBACK_SEASONS,
    decay: float = DECAY,
) -> pl.DataFrame:
    """
    Each RB/WR/TE's recency-weighted target share (their share of their
    primary team's total targets that season), UNNORMALIZED — see
    TARGET_SHARE_ADJUSTMENT_STRENGTH in config.py for why the ratio-to-
    position-average is computed later, against the currently-relevant
    player pool rather than this whole historical universe.

    Pulls team-level total targets via nfl.load_team_stats
    (summary_level="reg" — regular season only, same reasoning as the
    postseason-mixing fix in compute_fantasy_points.py) over the same
    season window, joined on (season, team) using each player-season's
    primary_team (see compute_fantasy_points.py's build_season_summary).
    """
    history = season_summary.filter(
        (pl.col("season") < current_season)
        & pl.col("position").is_in(["RB", "WR", "TE"])
    )
    history = history.with_columns(
        (pl.lit(current_season) - pl.col("season")).alias("seasons_ago")
    )
    history = history.filter(pl.col("seasons_ago") <= lookback_seasons)
    history = history.with_columns(
        (pl.lit(decay) ** pl.col("seasons_ago")).alias("weight")
    )

    last_completed_season = current_season - 1
    seasons = list(range(last_completed_season - lookback_seasons + 1, last_completed_season + 1))
    team_stats = nfl.load_team_stats(seasons=seasons, summary_level="reg")
    team_targets = team_stats.select(["season", "team", pl.col("targets").alias("team_targets")])

    history = history.join(team_targets, on=["season", "team"], how="left")
    history = history.with_columns(
        (pl.col("targets") / pl.col("team_targets")).alias("target_share")
    )
    history = history.filter(pl.col("target_share").is_not_null())

    weighted = history.group_by("player_id").agg(
        (
            (pl.col("target_share") * pl.col("weight")).sum() / pl.col("weight").sum()
        ).alias("weighted_target_share")
    )
    return weighted


def apply_target_share_adjustment(
    projections: pl.DataFrame,
    target_share: pl.DataFrame,
    strength: float = TARGET_SHARE_ADJUSTMENT_STRENGTH,
) -> pl.DataFrame:
    """
    Multiplies RB/WR/TE projected_fantasy_points by a factor derived
    from the player's own target-share ratio vs. the (currently-
    rostered) position average — see load_target_share and
    TARGET_SHARE_ADJUSTMENT_STRENGTH in config.py. Refines
    apply_team_offense_adjustment (which boosts every pass-catcher on a
    team identically) with each player's own share of that team's
    passing volume. QB and players with no target history (e.g. a true
    rookie, or a pure between-the-tackles back with ~0 targets) get
    1.0x, not an exclusion.
    """
    projections = projections.join(target_share, on="player_id", how="left")

    projections = projections.with_columns(
        _points_weighted_position_average("weighted_target_share").alias("position_avg_target_share")
    )
    projections = projections.with_columns(
        (pl.col("weighted_target_share") / pl.col("position_avg_target_share")).alias("target_share_ratio")
    )

    projections = projections.with_columns(
        pl.when(pl.col("position").is_in(["RB", "WR", "TE"]))
        .then(1.0 + strength * (pl.col("target_share_ratio").fill_null(1.0) - 1.0))
        .otherwise(1.0)
        .alias("target_share_adjustment_factor")
    )

    projections = projections.with_columns(
        (pl.col("projected_fantasy_points") * pl.col("target_share_adjustment_factor")).alias(
            "projected_fantasy_points"
        )
    )
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

        print("Loading current team rosters...")
        player_teams = load_player_teams(current_season)

        print("Filtering to currently-rostered players (excludes free agents/retirees)...")
        if player_teams is None:
            print(f"Warning: no roster data found for {current_season} — skipping the currently-rostered filter entirely.")
        else:
            # DEF is exempt: a team defense isn't "on a roster" or "a free
            # agent" the way an individual player is — all 32 teams are
            # always draftable as a DEF option. Its player_id is a team
            # abbreviation (e.g. "SF"), which would never match an
            # individual player_id from load_rosters() anyway — this is
            # an explicit bypass rather than relying on that mismatch to
            # silently exclude DEF from the filter's effect. K is NOT
            # exempt — kickers are individual rostered players and their
            # IDs do match load_rosters() normally.
            rostered_ids = set(player_teams["player_id"].to_list())
            before_count = projections.height
            projections = projections.filter(
                pl.col("player_id").is_in(list(rostered_ids)) | (pl.col("position") == "DEF")
            )
            print(f"  Kept {projections.height} of {before_count} players (dropped {before_count - projections.height} not on a current roster).")

        print("Applying position-specific age adjustment (evidence-based)...")
        projections = apply_age_adjustment(projections, ages)

        print("Loading team offensive strength (yards/game vs. league average)...")
        team_offense = load_team_offense_strength(current_season)

        print("Applying team offense adjustment...")
        projections = apply_team_offense_adjustment(projections, player_teams, team_offense)

        print("Loading QB rushing-yards emphasis...")
        qb_yards = load_qb_rushing_emphasis(season_summary, current_season)

        print("Applying QB rushing emphasis...")
        projections = apply_qb_rushing_emphasis(projections, qb_yards)

        print("Loading RB/WR/TE target share...")
        target_share = load_target_share(season_summary, current_season)

        print("Applying target share adjustment...")
        projections = apply_target_share_adjustment(projections, target_share)

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
        for pos in ["QB", "RB", "WR", "TE", "K", "DEF"]:
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
                        "team",
                        "team_offense_adjustment_factor",
                        "qb_rushing_emphasis_factor",
                        "target_share_adjustment_factor",
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