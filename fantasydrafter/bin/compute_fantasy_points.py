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

from lib.config import CURRENT_SEASON_OVERRIDE

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

# Skill positions this script handles with the SCORING dict above. K and
# DEF/DST use entirely different stat structures (field goals, points
# allowed, etc.) and are handled by their own functions below.
SKILL_POSITIONS = ["QB", "RB", "WR", "TE"]

# ---------------------------------------------------------------------------
# Kicker scoring (ESPN default — confirmed against the actual league
# settings screen). nflreadpy's load_player_stats() already buckets FG
# makes into these exact distance bands (fg_made_0_19/20_29/30_39/40_49/
# 50_59/60_) — confirmed by inspecting real 2023-2024 data before writing
# this, not assumed. A miss is a flat -1 regardless of distance, and that
# includes a BLOCKED attempt (fg_blocked) — the league settings only list
# one "missed FG" rule, not a separate blocked-kick rule, so a block is
# being treated as a missed kick for scoring purposes. Missed PATs aren't
# penalized (ESPN default), only missed FGs are.
# ---------------------------------------------------------------------------
KICKER_FG_MADE_POINTS = {
    "fg_made_0_19": 3.0,
    "fg_made_20_29": 3.0,
    "fg_made_30_39": 3.0,
    "fg_made_40_49": 4.0,
    "fg_made_50_59": 5.0,
    "fg_made_60_": 5.0,
}
KICKER_MISSED_FG_POINTS = -1.0  # applies per miss AND per block, flat
KICKER_PAT_MADE_POINTS = 1.0

# ---------------------------------------------------------------------------
# Defense/Special Teams scoring — confirmed directly from the actual
# league settings screen (not ESPN "default", this league's real config).
# Column mapping confirmed against real 2023-2024 team_stats data before
# writing this (see PROJECTNOTES.md for the columns that turned out to be
# red herrings, e.g. `def_fumbles` was always 0 — `fumble_recovery_opp`
# is the real "defense recovered the opponent's fumble" column).
#
# `def_tds` (turnover/blocked-kick return TDs) and `special_teams_tds`
# (kickoff/punt return TDs) are summed together at a single 6pt rate —
# the league scores all of those categories identically, so there's no
# need to split them even though the settings screen lists them as
# separate line items.
#
# KNOWN GAP: the rare "1-point safety" rule (blocking a PAT back into
# your own end zone) has no identifiable dedicated column in nflreadpy's
# team_stats — not implemented. This has happened only a handful of
# times in NFL history, negligible impact.
# ---------------------------------------------------------------------------
DEF_SACK_POINTS = 1.0
DEF_INTERCEPTION_POINTS = 2.0
DEF_FUMBLE_RECOVERY_POINTS = 2.0
DEF_SAFETY_POINTS = 2.0
DEF_TWO_POINT_RETURN_POINTS = 2.0
DEF_BLOCKED_KICK_POINTS = 2.0  # blocked punt/PAT/FG, non-TD
DEF_TOUCHDOWN_POINTS = 6.0  # any defensive/ST return TD


def _points_allowed_tier_expr() -> pl.Expr:
    pa = pl.col("points_allowed")
    return (
        pl.when(pa == 0)
        .then(5.0)
        .when(pa <= 6)
        .then(4.0)
        .when(pa <= 13)
        .then(3.0)
        .when(pa <= 17)
        .then(1.0)
        .when(pa <= 27)
        .then(0.0)
        .when(pa <= 34)
        .then(-1.0)
        .when(pa <= 45)
        .then(-3.0)
        .otherwise(-5.0)
    )


def _yards_allowed_tier_expr() -> pl.Expr:
    ya = pl.col("yards_allowed")
    return (
        pl.when(ya < 100)
        .then(5.0)
        .when(ya <= 199)
        .then(3.0)
        .when(ya <= 299)
        .then(2.0)
        .when(ya <= 349)
        .then(0.0)
        .when(ya <= 399)
        .then(-1.0)
        .when(ya <= 449)
        .then(-3.0)
        .when(ya <= 499)
        .then(-5.0)
        .when(ya <= 549)
        .then(-6.0)
        .otherwise(-7.0)
    )


def load_historical_stats(max_seasons_back: int = 20) -> pl.DataFrame:
    """
    Load weekly player stats for as many seasons as nflreadpy has available,
    up to max_seasons_back. Requirements call for weighting as much history
    as possible, defaulting to a ~20-season window if a cutoff is needed.

    CURRENT_SEASON_OVERRIDE is the season we're drafting FOR (may be
    unplayed/in-progress — no stats file exists for it yet on nflverse), so
    the pull window ends at the last completed season (CURRENT_SEASON_OVERRIDE
    - 1), not CURRENT_SEASON_OVERRIDE itself.

    Filtered to season_type == "REG": load_player_stats() returns postseason
    weeks mixed in with regular-season ones (season_type "POST"), which this
    redraft league doesn't play through — including them silently inflated
    games_played past the real 17-game regular-season max for any player
    whose team made a playoff run (e.g. a rookie showing 21 "games" played
    in a single season) and diluted fantasy_points_per_game with playoff-
    specific performance (tougher opponents, different game script) blended
    into what should be a pure regular-season rate.
    """
    last_completed_season = CURRENT_SEASON_OVERRIDE - 1
    seasons = list(range(last_completed_season - max_seasons_back + 1, last_completed_season + 1))

    stats = nfl.load_player_stats(seasons=seasons, summary_level="week")
    stats = stats.filter(pl.col("season_type") == "REG")
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

    Also retains season totals for targets/rushing_yards/passing_yards
    (raw counting stats, not points) and each player's primary team for
    the season — needed downstream for target-share (RB/WR/TE) and
    rushing-yards-emphasis (QB) adjustments, which need more than just
    the points total. games_played/fantasy_points_total already
    correctly sum across a mid-season trade (grouped by player+season
    only, not team); primary_team picks whichever team they played the
    most games for that season as an approximation for a traded player
    — not exact for a genuine 50/50 split, but that's a rare edge case.
    """
    skill = weekly.filter(pl.col("position").is_in(SKILL_POSITIONS))

    primary_team = (
        skill.group_by(["player_id", "season", "team"])
        .agg(pl.len().alias("games_with_team"))
        .sort("games_with_team", descending=True)
        .group_by(["player_id", "season"], maintain_order=True)
        .first()
        .select(["player_id", "season", "team"])
    )

    season_summary = (
        skill.group_by(["player_id", "player_display_name", "position", "season"])
        .agg(
            [
                pl.len().alias("games_played"),
                pl.col("fantasy_points").sum().alias("fantasy_points_total"),
                pl.col("fantasy_points").mean().alias("fantasy_points_per_game"),
                pl.col("targets").fill_null(0).sum().alias("targets"),
                pl.col("rushing_yards").fill_null(0).sum().alias("rushing_yards"),
                pl.col("passing_yards").fill_null(0).sum().alias("passing_yards"),
            ]
        )
        .join(primary_team, on=["player_id", "season"], how="left")
        .sort(["season", "fantasy_points_total"], descending=[True, True])
    )
    return season_summary


def compute_kicker_points(stats: pl.DataFrame) -> pl.DataFrame:
    """
    Adds `fantasy_points` to weekly K rows using the KICKER_* scoring
    above. `stats` is the same weekly pull used for skill positions —
    kicking stats are unified into load_player_stats() now, no separate
    stat_type argument needed (confirmed against the actual columns
    before writing this, not assumed).
    """
    k = stats.filter(pl.col("position") == "K")

    fg_points = pl.lit(0.0)
    for col, points in KICKER_FG_MADE_POINTS.items():
        if col in k.columns:
            fg_points = fg_points + pl.col(col).fill_null(0) * points
        else:
            print(f"Warning: expected kicker column '{col}' not found — skipping.")

    missed_fg_points = (
        pl.col("fg_missed").fill_null(0) + pl.col("fg_blocked").fill_null(0)
    ) * KICKER_MISSED_FG_POINTS
    pat_points = pl.col("pat_made").fill_null(0) * KICKER_PAT_MADE_POINTS

    return k.with_columns(
        (fg_points + missed_fg_points + pat_points).alias("fantasy_points")
    )


def build_kicker_season_summary(weekly_k: pl.DataFrame) -> pl.DataFrame:
    """
    Same output shape as build_season_summary() so K rows can be
    concatenated with skill-position rows downstream. Unlike skill
    positions, team is just .first() per player-season rather than "most
    games played with" — kicker mid-season trades are rare enough that
    the extra precision isn't worth it, and team isn't actually used by
    any K-specific adjustment anyway (team-offense-adjustment sources its
    player->team mapping from load_player_teams()/load_rosters() in
    projection_model.py, not from this column).
    """
    return (
        weekly_k.group_by(["player_id", "player_display_name", "position", "season"])
        .agg(
            [
                pl.len().alias("games_played"),
                pl.col("fantasy_points").sum().alias("fantasy_points_total"),
                pl.col("fantasy_points").mean().alias("fantasy_points_per_game"),
                pl.col("team").first().alias("team"),
            ]
        )
        .with_columns(
            [
                pl.lit(0).alias("targets"),
                pl.lit(0).alias("rushing_yards"),
                pl.lit(0).alias("passing_yards"),
            ]
        )
        .select(
            [
                "player_id", "player_display_name", "position", "season",
                "games_played", "fantasy_points_total", "fantasy_points_per_game",
                "targets", "rushing_yards", "passing_yards", "team",
            ]
        )
    )


def load_defense_weekly_stats(seasons: list[int]) -> pl.DataFrame:
    """
    Team-level DEF/ST weekly stats, with points_allowed and yards_allowed
    derived. nfl.load_team_stats() only has a team's own offense/defense
    PLAY stats (its own passing_yards, its own def_sacks, etc.) — NOT
    "points allowed" or "yards allowed" directly, confirmed by inspecting
    the actual schema before writing this (there's no points/score column
    in team_stats at all). Points allowed comes from nfl.load_schedules()
    (each game's final score, reshaped from home/away into a per-team-per-
    game row). Yards allowed is the OPPONENT's own total_yards from
    team_stats for that same game_id — a team's own team_stats row only
    has ITS OWN offensive yards, not what it allowed, so this needs a
    self-join keyed on (game_id, opponent_team) = (game_id, team).
    """
    team_stats = nfl.load_team_stats(seasons=seasons, summary_level="week")
    team_stats = team_stats.filter(pl.col("season_type") == "REG")
    team_stats = team_stats.with_columns(
        (pl.col("passing_yards") + pl.col("rushing_yards")).alias("total_yards")
    )

    opponent_yards = team_stats.select(
        ["game_id", "team", pl.col("total_yards").alias("yards_allowed")]
    )
    team_stats = team_stats.join(
        opponent_yards,
        left_on=["game_id", "opponent_team"],
        right_on=["game_id", "team"],
        how="left",
    )

    schedules = nfl.load_schedules(seasons=seasons)
    home_pa = schedules.select(
        ["game_id", pl.col("home_team").alias("team"), pl.col("away_score").alias("points_allowed")]
    )
    away_pa = schedules.select(
        ["game_id", pl.col("away_team").alias("team"), pl.col("home_score").alias("points_allowed")]
    )
    points_allowed = pl.concat([home_pa, away_pa])
    team_stats = team_stats.join(points_allowed, on=["game_id", "team"], how="left")

    return team_stats


def compute_defense_points(team_stats: pl.DataFrame) -> pl.DataFrame:
    """
    Adds `fantasy_points` per team-week using the DEF_* scoring above —
    both allowed-tiers apply simultaneously (additive), confirmed from
    the actual league settings screen. See the module-level comment block
    above DEF_SACK_POINTS for the column-mapping decisions this made
    (fumble_recovery_opp vs. def_fumbles, def_tds + special_teams_tds).
    """
    play_points = (
        pl.col("def_sacks").fill_null(0) * DEF_SACK_POINTS
        + pl.col("def_interceptions").fill_null(0) * DEF_INTERCEPTION_POINTS
        + pl.col("fumble_recovery_opp").fill_null(0) * DEF_FUMBLE_RECOVERY_POINTS
        + pl.col("def_safeties").fill_null(0) * DEF_SAFETY_POINTS
        + pl.col("def_2pt_made").fill_null(0) * DEF_TWO_POINT_RETURN_POINTS
        + (
            pl.col("def_punt_blocks").fill_null(0)
            + pl.col("def_pat_blocks").fill_null(0)
            + pl.col("def_fg_blocks").fill_null(0)
        )
        * DEF_BLOCKED_KICK_POINTS
        + (pl.col("def_tds").fill_null(0) + pl.col("special_teams_tds").fill_null(0))
        * DEF_TOUCHDOWN_POINTS
    )

    return team_stats.with_columns(
        (play_points + _points_allowed_tier_expr() + _yards_allowed_tier_expr()).alias(
            "fantasy_points"
        )
    )


def build_defense_season_summary(weekly_def: pl.DataFrame, team_names: pl.DataFrame) -> pl.DataFrame:
    """
    Same output shape as build_season_summary(), so DEF rows can be
    concatenated with skill/K rows downstream. Team abbreviation stands
    in for player_id (e.g. "SF") since DEF is a team-level fantasy
    position, not an individual one; player_display_name reads like
    "49ers D/ST" (team_nick from nfl.load_teams(), matching how ESPN
    displays D/ST options) rather than the full "San Francisco 49ers".
    """
    return (
        weekly_def.group_by(["team", "season"])
        .agg(
            [
                pl.len().alias("games_played"),
                pl.col("fantasy_points").sum().alias("fantasy_points_total"),
                pl.col("fantasy_points").mean().alias("fantasy_points_per_game"),
            ]
        )
        .join(team_names, left_on="team", right_on="team_abbr", how="left")
        .with_columns(
            [
                pl.col("team").alias("player_id"),
                (pl.col("team_nick") + " D/ST").alias("player_display_name"),
                pl.lit("DEF").alias("position"),
                pl.lit(0).alias("targets"),
                pl.lit(0).alias("rushing_yards"),
                pl.lit(0).alias("passing_yards"),
            ]
        )
        .select(
            [
                "player_id", "player_display_name", "position", "season",
                "games_played", "fantasy_points_total", "fantasy_points_per_game",
                "targets", "rushing_yards", "passing_yards", "team",
            ]
        )
    )


def main(force_recompute: bool = False):
    from pathlib import Path

    out_path = Path("lib/fantasy_points_by_season.csv")

    if out_path.exists() and not force_recompute:
        print(f"{out_path} already exists — skipping recompute.")
        print("(pass --force to recompute anyway)")
        season_summary = pl.read_csv(out_path)
    else:
        print("Loading historical player stats (this may take a while on first run)...")
        weekly_stats = load_historical_stats(max_seasons_back=20)
        seasons = sorted(weekly_stats["season"].unique().to_list())

        print("Computing Full PPR fantasy points per game (skill positions)...")
        weekly_with_points = compute_fantasy_points(weekly_stats)
        print("Building skill-position season summary...")
        skill_summary = build_season_summary(weekly_with_points)

        print("Computing kicker fantasy points...")
        weekly_k_points = compute_kicker_points(weekly_stats)
        print("Building kicker season summary...")
        kicker_summary = build_kicker_season_summary(weekly_k_points)

        print("Loading team defense stats (this may take a while)...")
        weekly_def_stats = load_defense_weekly_stats(seasons)
        print("Computing defense fantasy points...")
        weekly_def_points = compute_defense_points(weekly_def_stats)
        print("Building defense season summary...")
        team_names = nfl.load_teams().select(["team_abbr", "team_nick"])
        defense_summary = build_defense_season_summary(weekly_def_points, team_names)

        season_summary = pl.concat(
            [skill_summary, kicker_summary, defense_summary], how="diagonal_relaxed"
        )

        season_summary.write_csv(out_path)
        print(f"Done. Wrote {season_summary.height} player-season rows to {out_path}")

    # Quick sanity check — top 10 player-seasons by total fantasy points,
    # across ALL seasons (not just the most recent one). This re-sorts by
    # points alone; season_summary itself stays sorted by season for the
    # CSV output since later steps will want it grouped that way.
    print("\nTop 10 player-seasons of all time (by total fantasy points):")
    print(season_summary.sort("fantasy_points_total", descending=True).head(10))


if __name__ == "__main__":
    import sys

    main(force_recompute="--force" in sys.argv)