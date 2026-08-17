"""
config.py

Tunable settings for the projection model, kept separate from
projection_model.py so they're easy to find and adjust without digging
through the data pipeline logic.
"""

"""
1. Recency-weighted average (the core of the projection)
For each player, look back up to LOOKBACK_SEASONS (5) past seasons. Each season's per-game fantasy point average gets a weight based on how recent it is:

weight = DECAY ** seasons_ago

With DECAY = 0.65, last season gets weight 1.0, two seasons ago gets 0.65, three seasons ago gets 0.42, and so on — so it's a smooth taper toward the past rather than a hard cutoff. We use per-game points rather than season totals specifically so a great player who missed 6 games with an injury isn't penalized as if they were just mediocre — their rate is preserved, and games missed gets handled separately in the next step.

2. Projected games played
Same recency-weighted average, but applied to games_played per season instead of points, capped at 17 (a full season). This is a crude proxy for injury risk — a player who's missed time recently gets a lower projected games number, which drags down their total even if their per-game rate is elite.

3. Combine into a raw projection

projected_fantasy_points_raw = projected_ppg × projected_games

4. Age adjustment
Using birth dates, we compute each player's age going into the season, then apply a position-specific decline curve (defined in config.py) — e.g., RBs start declining past age 27 at 6%/year, QBs past 32 at 2%/year. Below the peak age, no adjustment. This multiplies onto the raw projection from step 3. These curve numbers are hand-picked assumptions based on general fantasy/NFL aging knowledge, not fitted to your data — a reasonable next improvement would be to actually backtest and calibrate them.

5. Position rank
Just an ordinal rank of projected_fantasy_points within each position (QB1, QB2, RB1, etc.) — used for both display and the next step.

6. ADP proxy & value flag
Since nflreadpy has no real ADP data, we approximate "what the market would think" using each player's rank from their single most recent completed season's total points. Comparing that to the model's own rank gives value_vs_adp_proxy: positive means the model likes the player more than their recent real-world finish would suggest (a potential "value" pick), negative means the opposite (the model is more skeptical than last year's results, often due to age/injury-risk adjustments pulling them down).

Where this is weakest, worth knowing going in:

No opportunity/role change signal (new team, new offensive coordinator, contract situation) — it's purely stats-derived.
The ADP proxy is a real approximation, not real draft-market data — it won't catch hype-driven ADP shifts (e.g., a rookie WR who didn't play last season getting drafted highly this year based on preseason buzz).
Age curves and decay rate are assumptions, not calibrated — good candidates for backtesting once you have a working pipeline end to end.
"""
# ---------------------------------------------------------------------------
# Season resolution
# ---------------------------------------------------------------------------
#
# `nfl.get_current_season()` has been unreliable for this pipeline's
# purposes — it's tied to nflreadpy's own notion of "current season" (e.g.
# it may not roll over to the upcoming season until the season actually
# kicks off in September), which doesn't match what we want here: as soon
# as a season's stats are final, we want it treated as the most recent
# completed season for projections, even months before the next season's
# games start. When they disagree, everything downstream silently drops
# the most recent completed season's data — exactly the players whose
# recent season should matter most (e.g. a breakout year) end up
# projected off stale, older data instead.
#
# Set this explicitly each year rather than trusting auto-detection.
# Should be the season you're drafting FOR (e.g. 2026 while prepping for
# the 2026 season, even before Week 1 has been played) — projections.py
# uses `season < CURRENT_SEASON_OVERRIDE`, so this must be one greater
# than the most recent completed season you want included.
CURRENT_SEASON_OVERRIDE = 2026

# ---------------------------------------------------------------------------
# Projection weighting
# ---------------------------------------------------------------------------
 
# How many of a player's most recent seasons to weight into the projection.
LOOKBACK_SEASONS = 5

# Exponential decay applied per season of recency. weight = DECAY ** n,
# where n=0 is the most recent season. Lower = more weight on recent data.
#
# Was 0.65 — bin/grid_search.py found 0.4 consistently outperforms it
# (mean rank correlation vs. actual season results) across every backtest
# window tested (2021-2023, 2021-2025, 2024-2025 alone), and it was the
# single most stable finding of the whole search — worth re-verifying via
# ./run_backtest.sh + ./run_backtest.sh with grid_search each future
# season to confirm it still holds as more seasons of data accumulate.
DECAY = 0.4
 
# Max games in a modern NFL regular season (used to cap projected games).
MAX_GAMES = 17
 
# ---------------------------------------------------------------------------
# Age adjustment
# ---------------------------------------------------------------------------
 
# Position-specific age curves: (peak_age, decline_per_year_past_peak).
# Below peak_age, no penalty is applied (rookies/young players aren't
# penalized — their limited sample already reflects less-established roles).
# This is intentionally simple; a real aging curve is nonlinear, but this
# gives a directionally correct adjustment without overfitting to a
# historical-only sample.
#
# Originally hand-picked from general NFL aging-curve knowledge (RB
# 27/0.06, WR 29/0.04, TE 29/0.03, QB 32/0.02 — RBs decline earliest and
# steepest, QBs latest and slowest). bin/grid_search.py backtested
# against 2021-2025 actual results and found notably lower peak ages
# outperform for RB/WR/TE, consistently across every window tested
# (2021-2023, 2021-2025, 2024-2025 alone) — the consistency is why
# these were adopted despite being a bigger jump from the original
# domain-knowledge assumption than expected. QB's curve barely moved
# (its own backtest signal was weak either way) so it was left as-is.
# Worth re-running the grid search each future season as more data
# accumulates — this is a small sample (5 draft classes) to be pulling
# aging curves out of.
#
# UPDATE: re-ran the search after fixing a real bug in it (Stage 3 was
# centering its search window on whatever's currently in this file, so
# rerunning it after applying a result let the window "walk" further
# every time — RB drifted 27->23->19 over two runs with no sign of
# converging, a search-methodology artifact, not a real signal). Fixed
# to a wide, FIXED, absolute grid (21-33) not centered on this file's
# current values. Result: it STILL hugs the edge of that range for
# RB/TE (found 21, the grid's minimum) even with the walking bug fixed —
# a second, independent sign of overfitting a small sample, not a
# genuine converged optimum. Not applying that result. Leaving these
# values as previously applied (above) rather than reverting to the
# original domain-knowledge numbers either, absent a clearer signal
# either way — but treat this whole knob as low-confidence until there's
# more backtest data to work with.
AGE_CURVES = {
    "RB": (23, 0.06),
    "WR": (25, 0.06),
    "TE": (25, 0.06),
    "QB": (32, 0.02),
}
 
 
def age_adjustment_factor(
    position: str, age: float | None, age_curves: dict = AGE_CURVES
) -> float:
    """
    age_curves defaults to the module-level AGE_CURVES but can be
    overridden — used by bin/grid_search.py to test alternate curves
    without mutating global config state.
    """
    if age is None or position not in age_curves:
        return 1.0
    peak_age, decline_per_year = age_curves[position]
    if age <= peak_age:
        return 1.0
    years_past_peak = age - peak_age
    factor = 1.0 - (decline_per_year * years_past_peak)
    return max(factor, 0.4)  # floor so projections don't go negative/absurd
 
 
# How much to dampen the age-based discount for a player who is still
# performing near their own recent peak rate (0 = no dampening, i.e. same
# as the flat age curve; 1 = fully dampened for a player still at peak).
# Addresses cases like Derrick Henry: age alone says "decline", but if
# their own recent performance doesn't show it, the discount shouldn't be
# as severe as for someone who has visibly already declined.
#
# Was 0.5 (picked to fix that specific case). bin/grid_search.py
# backtested against 2021-2025 actual results and found ~0.1 consistently
# scores better — i.e. the age curve itself (now lower peak ages, see
# AGE_CURVES) should mostly do the work, with less further dampening on
# top. This trades away some of the safety margin the original 0.5 gave
# specific cases like Henry/Barkley in exchange for better average
# accuracy — worth watching for regressions on similar "still performing
# despite age" players, and re-running the grid search each future season.
AGE_TREND_DAMPENING = 0.1
 
 
def evidence_based_age_adjustment(
    position: str,
    age: float | None,
    recent_ppg: float | None,
    peak_ppg_in_lookback: float | None,
    age_curves: dict = AGE_CURVES,
    age_trend_dampening: float = AGE_TREND_DAMPENING,
) -> float:
    """
    Like age_adjustment_factor, but scales the discount by how much the
    player has actually shown decline recently. A player whose most recent
    season's per-game rate is still near their own peak (within the
    lookback window) gets the discount dampened; one who has clearly
    already fallen off gets the full age-curve discount.

    age_curves/age_trend_dampening default to module-level config but can
    be overridden — used by bin/grid_search.py.
    """
    base_factor = age_adjustment_factor(position, age, age_curves)
    if base_factor >= 1.0 or not peak_ppg_in_lookback or not recent_ppg:
        return base_factor

    performance_ratio = min(recent_ppg / peak_ppg_in_lookback, 1.0)
    raw_discount = 1.0 - base_factor
    dampened_discount = raw_discount * (1.0 - age_trend_dampening * performance_ratio)
    return 1.0 - dampened_discount


# ---------------------------------------------------------------------------
# Team offense adjustment
# ---------------------------------------------------------------------------
#
# Players on a higher-volume offense (more total yards/game — passing +
# rushing) get a boost; players on a lower-volume offense get a
# discount. Uses the SAME recency-weighted lookback as the player-level
# projection (LOOKBACK_SEASONS/DECAY by default), applied to the team
# the player is CURRENTLY rostered with — this is a context adjustment
# layered on top of the player's own history, not a per-historical-
# season correction (a player's own history stays tied to the team(s)
# they actually played for at the time).
#
# 0 = no effect (factor always 1.0); 1 = full pass-through of the team's
# yards-per-game ratio vs. league average over the same window.
#
# 0.3 confirmed by bin/grid_search.py (Stage 4) — landed on the same
# value across two separate runs against a fixed, well-bounded search
# grid (0.0-1.5), with a small but real accuracy improvement (mean rank
# correlation +0.0016-0.0021 depending on run). A more confident finding
# than the AGE_CURVES stage — see that comment above for why those
# results weren't trusted.
#
# Worth trying next: splitting by pass/rush yards per position (WR/TE/QB
# scale with passing volume, RB with rushing volume) instead of total
# yards for everyone — this v1 uses total yards uniformly, per the
# initial ask.
TEAM_OFFENSE_ADJUSTMENT_STRENGTH = 0.3


# ---------------------------------------------------------------------------
# Roster status exclusion
# ---------------------------------------------------------------------------
#
# Roster statuses (from load_rosters_weekly()), NOT the weekly injury/
# practice report (load_injuries()) — that table stops generating entries
# once a player is actually placed on injured reserve, so it misses exactly
# the cases we care about (e.g. a player who tore an ACL mid-season and is
# out for the year). Roster status correctly reflects that: confirmed via
# debug_roster_status.py, a player placed on IR shows "ACT" (active) before
# the injury and "RES" (reserve) afterward, through the rest of the season.
#
# A player whose most recent status is one of these is dropped from the
# cheat sheet entirely (not just discounted) — in a redraft league you
# can't roster someone who's out for the year, so leaving them in the
# player pool at a reduced value still misleadingly suggests they're
# draftable (e.g. Tyreek Hill's 2025 season-ending knee injury).
#
# NOTE: "RES" is being treated as a single bucket for now. It likely covers
# several sub-types (injured reserve, PUP, NFI, reserve/futures, etc.) that
# a finer read of the `status_description_abbr` column could distinguish
# later — worth revisiting if this exclusion seems wrong for a specific
# player whose "RES" status turns out not to be season-ending (e.g. a
# short-term IR stint they could return from before your draft).
EXCLUDED_ROSTER_STATUSES = {"RES"}

# A player is only actually dropped if their most recent roster status is
# in EXCLUDED_ROSTER_STATUSES for at least this many consecutive trailing
# weeks. A single most-recent-week check can't tell a genuine season-ending
# absence apart from a short late-season rest/shutdown — both just show
# "RES" in the last recorded week. Calibrated against two real cases found
# via debug_roster_status.py: Tyreek Hill was RES for 13 straight weeks
# (5-18) in 2025, a real season-ending injury; Rashee Rice was RES for only
# the final 2 weeks (17-18), a late-season shutdown with no bearing on his
# 2026 outlook — he should NOT be excluded, but a single-week check would
# have dropped him too.
MIN_RES_STREAK_WEEKS = 4
 
 
# ---------------------------------------------------------------------------
# League settings — defaults based on the user's actual league. Meant to be
# user-editable (e.g. exposed as form inputs in the eventual React app).
# ---------------------------------------------------------------------------
 
LEAGUE_SIZE = 14
 
# Guaranteed starting slots per position (non-FLEX). K and DEF use separate
# data/scoring not covered by this pipeline yet — included here for roster
# math completeness but VBD/projections don't currently cover them.
ROSTER_SLOTS = {
    "QB": 1,
    "RB": 2,
    "WR": 2,
    "TE": 1,
    "DEF": 1,
    "K": 1,
}
 
# How many FLEX slots per team, and which positions are FLEX-eligible.
FLEX_SLOTS = 1
FLEX_ELIGIBLE_POSITIONS = ["RB", "WR", "TE"]
 
BENCH_SLOTS = 7
IR_SLOTS = 1
 
# ---------------------------------------------------------------------------
# VBD / tiering
# ---------------------------------------------------------------------------
 
# A new tier starts when the point-gap between two consecutive players
# EXCEEDS BOTH of:
#   - TIER_GAP_PCT_THRESHOLD of the higher-ranked player's projected points
#   - TIER_MIN_GAP_POINTS, a flat point minimum
# applied across a position's ENTIRE ranked list (no candidate-pool cutoff
# — there used to be one, TIER_CANDIDATE_POOL=40, with everyone below it
# dumped into a single "beyond candidate pool" tier; that put 80-180
# players spanning a huge, genuinely meaningful point range into one tier
# for every position, since nothing beyond rank 40 ever got tiered at all).
#
# Both conditions are needed together:
#   - Percentage alone breaks down near the bottom of a position's list,
#     where points approach 0 (or go slightly negative for replacement-
#     level scrubs) — dividing by a near-zero number makes even a trivial
#     gap look huge, which would carve the deep waiver-wire tail into a
#     mess of tiny, meaningless tiers.
#   - A flat point minimum alone breaks down at the top, where a 5-point
#     gap is a real cliff between ~150-point WR3s but noise between
#     350-point elite WR1s (same reason a single global std-dev threshold
#     across the whole pool failed before — see git history).
# Together: percentage-based tiering through the meaningful range of a
# position (top ~50-120 players depending on position, verified against
# real output), collapsing back to one tier for the deep, genuinely flat
# replacement-level tail where TIER_MIN_GAP_POINTS keeps percentage noise
# from firing on trivial gaps.
TIER_GAP_PCT_THRESHOLD = 0.03
TIER_MIN_GAP_POINTS = 3
 