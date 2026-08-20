# Project Notes — Fantasy Draft Tool

Context for picking this project up in Claude Code. The pipeline and app
code is self-documenting (see docstrings/comments), but this covers the
*why* behind some decisions and a few known gotchas that aren't obvious
from the code alone.

## What this is

A fantasy football draft tool: Python pipeline (using `nflreadpy`) builds
Full PPR projections + Value-Based Drafting (VBD) + tiers from historical
NFL data, output as `cheat_sheet.csv`. A React app (Vite, deployed to
GitHub Pages) displays it as a draft-day cheat sheet.

League: 14 teams, Full PPR, snake draft, redraft (not keeper/dynasty).
Roster: 1 QB, 2 RB, 2 WR, 1 TE, 1 FLEX, 1 DEF, 1 K, 7 bench, 1 IR.

## Pipeline order

1. `compute_fantasy_points.py` — pulls historical weekly stats, computes
   Full PPR fantasy points per player-season → `fantasy_points_by_season.csv`
2. `projection_model.py` — recency-weighted projections, age adjustment,
   roster-status adjustment, ADP proxy → `projections.csv`
3. `vbd_and_tiers.py` — VBD (replacement-level baseline per position,
   accounting for FLEX), tiers (gap-based within position) → `cheat_sheet.csv`
4. `fantasy-draft-app/scripts/csv_to_json.py` — converts `cheat_sheet.csv`
   → `fantasy-draft-app/public/data/cheat_sheet.json` for the React app

Each Python script caches its output CSV and skips recompute unless run
with `--force`. **Easy to forget** — if you change config.py or a script's
logic, you need `--force` on that script AND every script downstream of
it, or you'll be looking at stale numbers.

Convenience wrappers at the repo root: `./refresh_cheat_sheet.sh` (or
`npm run refresh-data` from `fantasy-draft-app/`) runs the full pipeline
+ regenerates the JSON in one shot. `./run_backtest.sh` runs the model
accuracy backtest (see below).

## Backtesting

`fantasydrafter/bin/backtest.py` (run via `./run_backtest.sh` or
`python -m bin.backtest` from `fantasydrafter/`) validates the model's
actual predictive accuracy: for each past season, it rebuilds what the
model would have projected using only data available before that season
— reusing projection_model.py's real functions directly, not a separate
reimplementation, so results always reflect whatever the pipeline
currently does — then compares against what players actually scored.
Reports point correlation, rank correlation, MAE/RMSE, and top-12 hit
rate, by position and year, and writes `lib/backtest_results.csv`.

Baseline results as of this model (last 5 completed seasons, 2021-2025):
point correlation ~0.75-0.80, rank correlation ~0.70-0.79, top-12 hit
rate ~40-65% depending on position/year. 2023 was a noticeably worse
year across QB/RB (correlation dropped to ~0.64/0.66) — not yet
investigated, worth digging into if revisiting the model.

Does not backtest VBD/tiers — those are draft-strategy overlays with no
ground truth to check against, not predictions in themselves.

## Tuning (grid search)

`fantasydrafter/bin/grid_search.py` (run via `python -m bin.grid_search`
from `fantasydrafter/`) searches DECAY, AGE_TREND_DAMPENING, and
per-position AGE_CURVES against backtest.py's accuracy, staged (not one
full joint grid — combinatorially infeasible): LOOKBACK_SEASONS x DECAY
jointly first, then AGE_TREND_DAMPENING, then each position's AGE_CURVES
independently. Deliberately does NOT search LOOKBACK_SEASONS output,
TIER_GAP_PCT_THRESHOLD/VBD knobs, or EXCLUDED_ROSTER_STATUSES/
MIN_RES_STREAK_WEEKS — no ground truth for the first two, and the last
would just reward excluding hard-to-predict players rather than genuine
accuracy.

**Applied from the first tuning pass** (searched against 2021-2025,
cross-checked against 2021-2023 and 2024-2025 alone for stability before
applying anything):
- `DECAY`: 0.65 → 0.4. The single most consistent finding — won in
  every window tested. High confidence this is real, not noise.
- `LOOKBACK_SEASONS`: left at 5 — the search result bounced between 3-5
  depending on the window (noisy), and matters less anyway once DECAY
  is already this low.
- `AGE_TREND_DAMPENING`: 0.5 → 0.1, and `AGE_CURVES` peak ages dropped
  notably (RB 27→23, WR/TE 29→25, QB unchanged) — consistent direction
  across every window, but in real tension with the deliberate
  Saquon-Barkley/Derrick-Henry fix that 0.5 existed for. Concretely:
  Barkley's age_adjustment_factor went 0.896 → 0.632, Henry's
  0.783 → 0.460, under the new config. Applied anyway — overall backtest
  accuracy improved (rank correlation 0.788 → 0.800, MAE 41.3 → 39.6) —
  but this is a real, deliberate tradeoff, not a free win. Worth
  re-running the grid search each future season as more data
  accumulates, and watching for other "still-productive older player"
  cases getting discounted too aggressively.

**Stage 3 (AGE_CURVES) methodology bug found on the second tuning pass,
now fixed — but the underlying tuning wasn't re-applied.** The search
was centering its grid on whatever's currently in config.py, so rerunning
it after applying a result let the window walk further every time (RB
peak_age drifted 27→23→19 across two runs, no sign of converging) — a
search-methodology artifact, not a real signal. Fixed to a wide, FIXED,
absolute grid (peak_age 21-33) that doesn't depend on the file's current
state. Even fixed, the result still hugs the edge of that range for
RB/TE (found 21, the grid's minimum) — a second, independent sign of
overfitting the small 5-year sample rather than a genuine converged
optimum. Left AGE_CURVES at the previously-applied values rather than
pushing further or reverting — **treat this whole knob as low-confidence
until there's more backtest data**, and don't trust a future
`grid_search.py` Stage 3 run just because it's "consistent" across a
couple of windows — as this episode showed, a methodology bug can look
consistent too.

**UPDATE**: RB's grid-search-retuned value has since been reverted back
to the original domain-knowledge curve (27, 0.06) — this low-confidence
flag turned out to be justified, not just theoretical caution. See
"Projection accuracy diagnostic + fixes" below for the full story
(confirmed via a named-player complaint, validated with a full backtest
rerun). WR and TE remain at the grid-search values, still flagged
low-confidence exactly as before.

## Team offense adjustment

Players on a higher-volume offense (more total yards/game — passing +
rushing) get a projection boost; players on a lower-volume offense get a
discount. `load_team_offense_strength()` in `projection_model.py` pulls
`nfl.load_team_stats(summary_level="reg")` (regular season only, same
reasoning as the postseason-mixing fix below), computes each team's
recency-weighted yards/game (same DECAY/LOOKBACK_SEASONS shape as the
player-level projection, but NOT tied to the same override — a
grid-search sweep of the player-level DECAY doesn't also move the
team-level signal), and expresses it as a ratio to league average.
`TEAM_OFFENSE_ADJUSTMENT_STRENGTH` (config.py) controls how much this
ratio actually moves a player's projection (0 = no effect, 1 = full
pass-through) — confirmed at **0.3** via `bin/grid_search.py` Stage 4,
consistent across two separate runs against a fixed grid, small but real
accuracy improvement.

v1 uses TOTAL yards uniformly for every position — the initial ask
didn't specify a split. Worth trying next: passing yards for WR/TE/QB,
rushing yards for RB, since a run-first team's total yards can look fine
while still being a bad environment for its receiving corps specifically.

## QB rushing emphasis + target share adjustment

Two more player-level signals, added together to fix a concrete problem:
QB tier 2 had 12 players bunched together (Jared Goff/Matthew Stafford/
Dak Prescott mixed in with Jalen Hurts/Lamar Jackson/Bo Nix — very
different real fantasy profiles) because projected_fantasy_points alone
wasn't spreading them out enough for the tiering threshold to ever fire
— the same "flat curve" issue already fixed for WR, but compounded here
by QB rushing production not being weighted heavily enough as a
differentiator.

- **QB rushing emphasis** (`load_qb_rushing_emphasis`/
  `apply_qb_rushing_emphasis` in `projection_model.py`): each QB's
  recency-weighted rushing production relative to the QB position
  average, applied as a multiplicative boost/discount on top of
  projected_fantasy_points. Deliberately an EXTRA emphasis, not
  re-counting points already earned — rushing yards already count
  toward a QB's historical fantasy points the same as everyone else's.
  **UPDATE — real bug found and fixed (Goff diagnostic)**: this
  originally summed `passing_yards + QB_RUSHING_YARDS_WEIGHT x
  rushing_yards`, which was dominated by `passing_yards` for any
  high-volume passer regardless of actual rushing — a pure pocket
  passer in a big-volume offense (Jared Goff) got nearly as much
  "rushing emphasis" credit as a real dual-threat QB, double-counting
  yards already earned via passing. Fixed to use `rushing_yards` alone;
  `QB_RUSHING_YARDS_WEIGHT` no longer exists (mathematically inert once
  passing_yards was removed — see "Projection accuracy diagnostic +
  fixes" below for the full writeup and the re-tuned
  `QB_RUSHING_EMPHASIS_STRENGTH=0.05`, down from 0.3).
- **Target share adjustment** (`load_target_share`/
  `apply_target_share_adjustment`): each RB/WR/TE's recency-weighted
  share of their team's total targets, relative to the position average
  — refines the team offense adjustment (which boosts every pass-catcher
  on a team identically) with each player's own share of that team's
  passing volume, differentiating a true WR1 from a same-team WR3, or a
  receiving back from a between-the-tackles committee runner.
  `TARGET_SHARE_ADJUSTMENT_STRENGTH=0.3` (grid-searched, confirmed
  initial guess, genuine interior optimum — 1.5 was tested and lost).

Both required extending `compute_fantasy_points.py`'s `build_season_summary()`
to retain raw targets/rushing_yards/passing_yards per player-season (previously
discarded after computing fantasy points) plus each player's primary team for
the season — required a `--force` recompute of `fantasy_points_by_season.csv`.

**Real bug found and fixed while building this**: the position-average
baseline for both adjustments was originally a simple per-player mean
across the currently-rostered pool at a position. That's still wrong —
every roster carries a long tail of 2nd/3rd-string backups with
near-zero relevant usage (a backup QB who's thrown 10 career passes, a
committee-piece RB with 3 career targets), and averaging them in
alongside real starters drags the baseline way down, which in turn
inflates every real starter's ratio far past anything reasonable (found
empirically: Josh Allen's qb_yards_ratio came out at 2.8x, Bijan
Robinson's target_share_ratio at 4.5x under a simple mean — his
projected points came out at 662, roughly double a sane top-RB
projection). Fixed with `_points_weighted_position_average()` — weights
each player's contribution to the baseline by their own (pre-adjustment)
projected_fantasy_points, so irrelevant bench players barely move it.
Shared by both adjustments.

**Backtest results**: per-position rank correlation improved in most
cells (QB, RB, TE, WR all individually up in the grid search's own
unweighted-mean-across-cells metric, +0.0028 overall on that metric).
The backtest.py "OVERALL ACCURACY" pooled correlation (all 2229
player-seasons combined) came out essentially flat (~0.8005, statistically
indistinguishable from before) — that number is dominated by WR's larger
sample size and isn't the actual objective these knobs were tuned
against, so don't read too much into it moving or not moving; the
per-position, per-cell numbers are the more meaningful comparison here.
The QB tiering problem this was built to fix is visibly resolved: tier 2
dropped from 12 players to 5, with the dual-threat/pocket-passer split
now reflected in the ordering (Bo Nix/Hurts ahead of Stafford/Prescott,
which wasn't true before).

## Projection accuracy diagnostic + fixes — Goff / Barkley / Evans / 2023 dip

Triggered by three specific complaints (Goff QB4 too high, Barkley RB14
too low, Evans too low among WRs) plus the long-logged, never-explained
2023 QB/RB backtest dip. Diagnosed first with zero code changes (every
diagnostic field needed was already present in the cached CSVs), THEN
two confirmed-real issues were fixed and validated via full backtest
reruns before being accepted — the 2023 dip turned out not to be
fixable at all (see its own section below).

### Jared Goff (QB4) — fixed, diverges from the initial hypothesis

The original guess was "TD rate isn't being regressed toward a
sustainable mean." Checked directly: his TD rate has climbed for two
straight seasons (2021-2025: 3.8% → 4.9% → 5.0% → 6.9% → 5.9%) in a
stable scheme — not an obvious one-year fluke, so TD-rate regression
isn't a clean explanation for him specifically.

The actual driver was `qb_rushing_emphasis_factor` = **1.094** (a +9.4%
boost on top of an already-strong raw projection), and the mechanism was
a real formula flaw: `emphasized_yards = passing_yards + 3×rushing_yards`
was dominated by `passing_yards` for any high-volume passer, rushing or
not. Checked Goff's actual recency-weighted emphasized yards (~4,718)
against real dual-threat QBs — it was comparable to or **higher than
Lamar Jackson's** (~4,709, though his 2025 was injury-shortened) and not
far off Josh Allen's (~5,433). A pure pocket passer in a high-volume
offense was getting nearly as much "rushing emphasis" credit as an
actual rushing QB, on top of already getting full credit for that same
passing volume in his base fantasy points — a real double-count,
diverging from the adjustment's own documented intent ("not just
re-counting the same points twice").

**Fixed**: `emphasized_yards` now uses `rushing_yards` alone — see the
"QB rushing emphasis" section above for the full writeup (formula
change, the now-removed `QB_RUSHING_YARDS_WEIGHT` knob turning out to be
mathematically inert, and the `QB_RUSHING_EMPHASIS_STRENGTH` re-tune
from 0.3 to 0.05). **Result**: Goff QB4 → **QB6**, with his
`qb_rushing_emphasis_factor` now a mild 0.96 (a small, appropriate
discount) instead of the old 1.09 boost. QB backtest accuracy improved
too (mean rank correlation 0.7069 → 0.7081 across 2021-2025, 3 of 5
years better, 1 worse, 1 flat) — a clean win, not a tradeoff.

### Saquon Barkley (RB14) and Mike Evans (too low among WRs) — confirms the AGE_CURVES overfitting concern, but the fix only applied to RB

Both were overwhelmingly driven by `age_adjustment_factor` — everything
else (team offense, target share) was within a few percent of neutral.
Recomputed both using the real `evidence_based_age_adjustment()`
function with the pre-grid-search curve values vs. the then-current
(grid-search-retuned) ones:

| Player | Old curve factor → points | Retuned curve factor → points | Rank swing |
|---|---|---|---|
| Barkley (age 29.6) | 0.857 → 248.3 pts | 0.632 → 183.3 pts (actual) | **RB14 → RB5** (isolated test) |
| Evans (age 33.0) | 0.849 → 120.0 pts | 0.548 → 77.5 pts (actual) | **WR63 → WR44** (isolated test) |

(Old curve: RB 27/0.06, WR 29/0.04 — the original domain-knowledge
values before the grid search retuned them to RB 23/0.06, WR 25/0.06.)
A single knob swinging both players by double-digit rank positions was
strong, concrete evidence the retuned curve was the dominant factor for
both complaints — not a red herring, and the exact overfitting risk
`AGE_CURVES` was already flagged with in the Tuning section above.

**But the full backtest told a more complicated story once both were
actually reverted together.** RB's cost was small and mixed (2 of 5
years better, 3 worse, mean -0.0044); **WR got WORSE in all 5 of 5
years tested** (mean -0.0084) — not noise, a consistent pattern.
Discussed directly and resolved: **reverted RB only, kept WR at the
grid-search value (25, 0.06)**. Barkley's complaint gets fixed; Evans
stays under-projected as a deliberately accepted limitation — a
33-year-old still-productive WR is a rare pattern in a 5-year sample,
and the grid-search curve appears to genuinely fit the broader WR
population better even though it mishandles this one outlier. With only
RB reverted, WR's backtest numbers came back to essentially the original
baseline (mean rank correlation 0.7633 → 0.7640, 0 years worse) —
confirming WR's problem really was specifically the WR curve change,
not some other interaction.

**Final applied config**: `AGE_CURVES` RB reverted to `(27, 0.06)`; WR
and TE both stay at the grid-search values `(25, 0.06)` (TE was never
touched at all — the diagnostic gathered zero direct evidence for TE).
**Result**: Barkley RB14 → **RB10** (a real fix, though gentler than the
isolated-test estimate of RB5 once combined with the actual final config
rather than a hypothetical single-knob change); Evans stays **WR63**,
unchanged, by design.

**A genuinely low-confidence knob surfaced along the way**:
`AGE_TREND_DAMPENING` was re-checked against the reverted curves (the
two interact) and turned out to be unstable, not just marginal — its
"winner" flip-flopped between 0.0 and 0.2 across three re-runs against
slightly different curve combinations during this investigation, with
every value in that range scoring within ~0.0006 of each other (compare
to the ~0.012 swing that justified the original 0.5→0.1 move — this is
nowhere near that kind of signal). Kept at the prior validated value
(0.1) rather than chase noise.

**Real bug found and fixed during this re-check**: `grid_search.py`'s
"unfiltered" search objective (used by Stages 1/2/4 and the baseline/
final reporting) was silently including K in the optimization target —
K/DEF rows exist in `season_summary` now (from the K/DEF work) but this
whole search predates that, and K isn't even affected by several of
these knobs at all (e.g. `AGE_TREND_DAMPENING` has zero effect on K —
it's not in `AGE_CURVES`). `backtest.py` already had a
`POSITIONS = ["QB", "RB", "WR", "TE"]` constant declared for apparently
exactly this purpose, but it was never actually wired up to filter
anything. Fixed by filtering `evaluate()`'s combined result to
`POSITIONS` in `grid_search.py` — this affects every future
`grid_search.py` run, not just this session's. Position-scoped calls
(e.g. `mean_rank_correlation(joined, position="QB")`) were never
affected by this bug, since they already narrowed to one position
explicitly — so the earlier-established DECAY/TEAM_OFFENSE_ADJUSTMENT_
STRENGTH/QB_RUSHING_YARDS_WEIGHT(now removed)/QB_RUSHING_EMPHASIS_
STRENGTH results all predate K/DEF's existence anyway and were never
contaminated by this.

**Overall backtest impact of everything in this section combined**
(rushing-emphasis fix + RB-only AGE_CURVES revert + AGE_TREND_DAMPENING
kept at 0.1): pooled rank correlation across all positions/years
0.8125 → 0.8123 (essentially flat, -0.0002). QB tiering (the original
reason the rushing-emphasis adjustment was built) spot-checked and
confirmed NOT regressed — tier 2 has 1 player, tier 3 has 3, tier 4 has
5, nothing close to the old 12-player mega-tier problem.

### 2023 QB/RB backtest dip — root cause found, but it's not a tunable knob

Quantitatively, 2023 wasn't dramatically noisier than other years in raw
terms (17.7% of QB/RB backtest players had >100pt absolute error vs.
13-16% in 2021/2022/2024/2025; mean abs error 54.0 vs. 47-50 elsewhere)
— rank correlation (0.60 QB / 0.63 RB, both the low point of the
5-year window) dropped more than error magnitude did because the misses
were concentrated **at the top of the rankings**, which rank correlation
is disproportionately sensitive to.

Rebuilt the actual 2023 backtest projection (`bin/backtest.py`'s
`build_backtest_projection`/`score_year`) and sorted by error to find
the specific players driving it. Both positions show the same
symmetric pattern:

- **QB**: Aaron Rodgers (projected #10, actual #75 — tore his Achilles
  on the 4th offensive snap of the season), Kyler Murray (#8→#26, still
  recovering from ACL), several veteran game-managers benched mid-season
  (Wentz, Mariota, Garoppolo, Darnold, Mac Jones) — all over-projected.
  Jordan Love (#58→#5), Brock Purdy (#29→#6), Sam Howell, Josh Dobbs,
  Gardner Minshew, Desmond Ridder all broke out from backup/unproven
  roles the model had no real track record to project from — all
  badly under-projected.
- **RB**: Austin Ekeler (#1→#26), Josh Jacobs (#3→#28), Fournette,
  Dalvin Cook, Aaron Jones all lost jobs or underperformed — this was
  the well-documented "veteran RB market crash" offseason. Kyren
  Williams (#79→#7), Raheem Mostert, Jerome Ford, Breece Hall, James
  Cook all broke out from committee/backup roles into lead-back volume
  nobody predicted.

**Conclusion: this is a role/opportunity-unpredictability problem, not
a scoring-efficiency problem** — it doesn't point at TD regression or
any tunable config knob. No stats-based recency-weighted model can
predict a Week 1 season-ending injury to an elite starter or a
mid-season benching in favor of an unproven backup. Closer to
irreducible variance than a bug: 2023 just had an unusually large
*concentration* of these events landing on players ranked at the top.
**No fix proposed or applied** — flagging this as explained/understood
rather than a mystery, not as something actionable.

## Kicker + Defense (K/DEF)

Both now covered end-to-end: `compute_fantasy_points.py` → `projection_model.py`
→ `vbd_and_tiers.py` → React app, same as QB/RB/WR/TE. Built from a
detailed design doc; scoring rules were confirmed directly against the
real league settings screen (not guessed), and data columns were
inspected live via nflreadpy before writing any scoring logic — same
"don't guess column names" discipline as the rest of this project, since
kicking/defense stats turned out to have several plausible-looking but
wrong column names (see below).

### Scoring

- **Kicker** (`compute_kicker_points()`): FG makes tiered by distance —
  0-39yd 3pt, 40-49yd 4pt, 50+yd 5pt — using nflreadpy's own distance-
  bucketed columns (`fg_made_0_19`/`20_29`/`30_39`/`40_49`/`50_59`/`60_`,
  confirmed present via live inspection, not assumed). Any miss is a
  flat -1 regardless of distance, **and this includes blocked kicks**
  (`fg_missed + fg_blocked`) — the league settings only list one "missed
  FG" rule, no separate blocked-kick rule. PAT made = 1pt; missed PATs
  aren't penalized (ESPN default). Kicking stats are unified into
  `load_player_stats()` now — no separate `stat_type` argument needed.
- **Defense/ST** (`compute_defense_points()`): play-based categories
  (sack 1pt, INT 2pt, fumble recovery 2pt, safety 2pt, defensive 2pt
  return 2pt, blocked kick 2pt, any defensive/ST return TD 6pt) plus
  points-allowed and yards-allowed tiers, **additive** (both tiers apply
  simultaneously — confirmed from the actual league settings, not an
  either/or). `nfl.load_team_stats()` only has a team's own offense/
  defense PLAY stats, not "points allowed" or "yards allowed" directly —
  those are derived: points allowed from `nfl.load_schedules()`'s
  per-game score (reshaped into a team-week row), yards allowed by
  self-joining `team_stats` on `(game_id, opponent_team)` to pull the
  OPPONENT's own total yards for that game.
- **Column names that turned out to be red herrings** (found by
  inspecting real 2023-2024 data before writing scoring logic, not
  assumed): `def_fumbles` was always 0 in the sample checked —
  `fumble_recovery_opp` is the real "defense recovered the opponent's
  fumble" column. `def_tds` (turnover/blocked-kick return TDs) and
  `special_teams_tds` (kickoff/punt return TDs) are two separate
  columns, but both score identically (6pt) in this league, so they're
  just summed together rather than split.
- **Known gap**: the rare "1-point safety" rule (blocking a PAT back
  into your own end zone) has no identifiable dedicated column in
  nflreadpy's team_stats — not implemented. Negligible impact (this has
  happened only a handful of times in NFL history).
- Sanity-checked against real 2024 results before trusting the numbers:
  top fantasy kickers (Chris Boswell, Brandon Aubrey) and top defenses
  (Broncos, Packers, Eagles, Vikings) both matched real-world 2024
  performance.

### Pipeline adjustments (`projection_model.py`)

- **Age adjustment**: DEF forced to `1.0` explicitly (a team has no age
  at all) rather than relying on age coming back `null` for a team-
  abbreviation "player_id" — both paths produce the same result, but the
  explicit version doesn't depend on that being an accident of two
  unrelated null-safety checks. K simply isn't a key in `AGE_CURVES`
  (config.py), so it already gets `1.0` the same way any uncovered
  position would — no special-casing needed there.
- **Currently-rostered filter**: DEF explicitly bypasses it (all 32
  teams are always "available" as a DEF option — a team isn't "on a
  roster" or "a free agent" the way a person is). This had to be
  explicit — DEF's player_id (a team abbreviation) would never match an
  individual player_id from `load_rosters()` anyway, which would have
  silently dropped every DEF row from the pool entirely if left
  unhandled. K is NOT exempt — kickers are individual rostered players
  and matched cleanly (confirmed: all 43 K IDs in a season's stats
  matched `load_rosters()`).
- **Roster-status (RES/injury) exclusion**: already works correctly for
  DEF with no changes needed — DEF's team-abbreviation "player_id" never
  matches an individual player's weekly roster status, so it comes back
  `null`, and the existing `fill_null(False)` on the exclusion mask
  means "no match" is never treated as "excluded."
- **Team offense adjustment**: not explicitly discussed in the design
  doc either way. K still gets it (its player_id matches
  `load_rosters()` normally, so a kicker's team affects its projection
  slightly). DEF doesn't (no match, defaults to `1.0`) — this happens
  by construction, no code change needed.

### K/DEF late-round push (the one real design decision made along the way)

Raw VBD for K/DEF is mathematically consistent with every other
position's methodology, but it doesn't match how anyone actually
drafts. Found by testing before shipping this, not assumed: with no
adjustment, the best kicker landed at **overall VBD rank ~49** (~round
4) and the best defense at **~67** (~round 5) — the recommendation
engine would have suggested drafting a kicker or defense way earlier
than any real drafter would.

Discussed directly and resolved as **a config-driven flat-point
discount applied only to the cross-position comparison** — never to the
`vbd` column/field itself, so K/DEF's own tab still shows true,
undiscounted VBD and tiers among themselves:

- `LATE_ROUND_POSITIONS = {"K", "DEF"}`, `LATE_ROUND_VBD_OFFSET = 300`
  in `config.py`. 300 is a flat point offset, not a percentage — picked
  so even the single BEST kicker/defense's discounted value still lands
  below the WORST rostered skill-position player's true VBD
  (skill-position VBD floor was ~-230 in the data checked; top K was
  ~62, top DEF ~43). A flat offset was necessary rather than a
  multiplier — K/DEF's VBD is positive while deep skill-position VBD is
  negative, and no positive multiplier can push a positive number below
  a negative one.
- Applied in `vbd_and_tiers.py`'s `compute_vbd()` for `vbd_rank_overall`
  only. Mirrored in the React app as `crossPositionValue()` in
  `strategies.js` (same offset, same position set, **manually kept in
  sync** — there's no shared source of truth between the Python and JS
  sides here, worth double-checking both if this ever gets retuned),
  used by `robustRBScore`'s QB/TE/K/DEF fallback and the "also consider"
  raw-BPA comparison in `App.jsx`. Verified: with this in place, K/DEF
  no longer appear anywhere in the top 20 of the ALL tab, and the
  Recommended Pick callout never suggests either from a fresh draft
  state.
- **This calibration is deliberately aggressive** — it guarantees K/DEF
  rank below literally every skill-position player in the pool (600+
  players), not just "somewhere realistic like round 10-13." Found via
  further testing: combined with the existing `MAX_ROSTER_COUNTS` caps
  (QB:2/RB:6/WR:6/TE:2 = 16, which exceeds the roster's actual 14
  skill-position slots), simulating a full 16-round draft via "sort by
  strategy + always pick the top of the list" never drafted a K or DEF
  at all — skill positions stayed "eligible" the whole way through,
  since the recommendation engine has no concept of total roster
  capacity, only per-position caps.
- **Fixed** (originally shipped as a documented known limitation, then
  addressed on user request): the `recommendation` useMemo in `App.jsx`
  now has an explicit override that fires once your roster has
  `FULL_ROSTER_SIZE - 2` (14) or more players — if you're still missing
  a DEF or K at that point, the callout forces whichever missing one has
  the higher true VBD (comparing DEF vs. K directly by `vbd`, NOT the
  suppressed `crossPositionValue()` — the whole point of that
  suppression was "don't take this too early," which no longer applies
  once it's being deliberately forced) instead of the normal strategy
  pick, with `alsoConsider` replaced by a plain-language note ("you
  don't have a K yet — grab one now"). If only one of DEF/K is missing
  (e.g. you drafted one manually earlier), it forces that one
  specifically rather than comparing against the one you already have.
  **Real bug found and fixed right after shipping this**: it was
  originally scoped to the "Recommended:" callout only, NOT the "Sort
  by strategy" board ordering — but "sort by strategy + always pick the
  top of the list" (the user's actual workflow, not reading the callout
  text) is a completely separate code path in `rows`' useMemo that never
  got the same override. Without it, that workflow kept drafting skill
  players all the way to the `MAX_ROSTER_COUNTS` sum (16 skill picks)
  before DEF/K ever surfaced, landing them at picks 17-18 instead of
  15-16 — confirmed by the user hitting exactly this in a real
  playthrough. Fixed by applying the identical missing-DEF/K check
  inside the `sortByStrategy` sort comparator too, floating missing
  DEF/K to the very top (ahead of even a boosted RB/WR score) once the
  same 14+-roster-size condition is met. Re-verified end-to-end using
  the user's exact reported workflow (sort-by-strategy + autodraft +
  always click top of list, no reference to the callout at all): pick
  15 = K, pick 16 = DEF, final roster has exactly 1 of each.

### React app additions

- `POSITIONS` in `App.jsx` extended to `["QB","RB","WR","TE","K","DEF"]`
  — this alone adds K/DEF tabs (tier-grouped, position-ranked, same as
  every other position tab) since `TABS` is derived from it.
- `MAX_ROSTER_COUNTS`/`computeRosterCounts` in `strategies.js` extended
  with `K: 1, DEF: 1` — there's exactly one K slot and one DEF slot, so
  a 2nd of either is never useful in a redraft league.
- **My Roster tab**: `STARTING_SLOTS` restructured from two
  index-coupled parallel arrays (position list + hardcoded label list)
  into a single array of `{ position, label }` pairs — was fragile
  before (adding a slot meant keeping two arrays in sync by index), now
  each slot's label lives right next to what fills it. DEF and K added
  as their own dedicated starting slots (not FLEX-eligible, matching
  real roster rules). `FULL_ROSTER_SIZE` (draft-grade unlock threshold)
  is derived from this same array, so it moved from 14 to 16
  automatically — no separate number to keep in sync.
- New CSS chip colors `--pos-k` (muted blue-gray) and `--pos-def`
  (muted brown), following the same `--pos-*` custom-property pattern
  established for QB/RB/WR/TE.

## Known gotchas already hit (don't re-debug these)

- **`load_rosters()` and `load_team_stats()` disagree on Arizona's team
  code** (`AZ` vs `ARI`) — found empirically joining the two together
  (every Arizona player silently got no team-offense signal, no error,
  just fell back to the null-safe 1.0x default). Not documented anywhere
  in nflverse's own dictionaries. `TEAM_CODE_ALIASES` in
  `projection_model.py` normalizes it. Only mismatch found across all 32
  teams as of this check — but if a future nflreadpy update introduces
  another one, it'll fail the same silent way, not an error.

- **nflreadpy column names don't always match the docs.** Already found:
  `passing_interceptions` not `interceptions`; `load_rosters()` /
  `load_rosters_weekly()` ID column found dynamically (search for a
  column containing "gsis") rather than hardcoded, since we got burned
  guessing wrong more than once.
- **`load_injuries()` is the wrong table for catching season-ending
  injuries.** Weekly practice-report entries stop being generated once a
  player is actually placed on IR — so it never showed anything for a
  player who tore an ACL and missed the rest of the season.
  `load_rosters_weekly()`'s `status` column (`"ACT"` vs `"RES"`) is the
  right source for that instead.
- **`"Questionable"`/`"Doubtful"` are weekly game-day tags, not season
  signals** — including them in a discount multiplier caused false
  positives (e.g. Ja'Marr Chase got docked 10% for a single meaningless
  end-of-season tag). Only persistent statuses (`RES`, and season-roster
  absence) should carry a discount.
- **`nfl.get_current_season()` was resolving to an unexpected (too old)
  year.** It's tied to nflreadpy's own season rollover (may not flip to
  the upcoming season until games actually start in September), not "the
  season we're drafting for." This silently dropped the most recently
  completed season from the recency-weighted lookback (`season <
  current_season` excluded it) — confirmed as the actual cause of a real
  ranking bug: JSN and Rashee Rice were being ranked off 2023–2024 data
  only, missing their 2025 breakout/return seasons entirely, even though
  that data existed in `fantasy_points_by_season.csv`. Fixed by using
  `CURRENT_SEASON_OVERRIDE` in `config.py` in both
  `compute_fantasy_points.py` and `projection_model.py` instead of
  calling `get_current_season()` directly — **must be bumped by hand each
  year** (currently `2026`). Root cause of nflreadpy's own resolution
  behavior was never diagnosed, just bypassed.
- **Polars `group_by().first()` doesn't preserve sort order** unless you
  pass `maintain_order=True`. Hit this in `load_current_roster_status` —
  looked correct (sort then take first per group) but was silently
  unreliable without it.
- **Polars `rank()` returns an unsigned int type.** Subtracting two rank
  columns without casting to `Int64` first causes silent overflow into
  huge positive numbers instead of going negative.

## Modeling decisions worth knowing

- **Age adjustment is "evidence-based," not just age-based**: a flat
  age-curve discount unfairly penalized players (e.g. Saquon Barkley)
  who were still performing at an elite level, worse than players of a
  similar age with a similar decline curve but who'd also already shown
  real decline (e.g. Derrick Henry). The fix dampens the age discount in
  proportion to how close a player's most recent season is to their own
  peak in the lookback window. See `evidence_based_age_adjustment()` in
  `config.py`.
- **ADP is approximated, not real** — nflreadpy has no ADP data. The
  proxy uses each player's prior-season finish rank (position-scoped AND
  overall/cross-position versions both exist). This means it won't catch
  hype-driven real ADP shifts (rookies, contract situations, scheme
  changes) — it's a "what would raw recent performance suggest" signal,
  not real market sentiment.
- **Only QB/RB/WR/TE are covered.** K and DEF need entirely different
  data (field goals, points/yards allowed) and haven't been built yet.
- **Tiers are position-scoped** — "Tier 1" QB and "Tier 1" RB aren't
  comparable. The React app's "Overall" tab intentionally shows a flat
  ranked list instead of tier groups for this reason; tier grouping only
  applies on a single-position tab.
- **Players on long-term reserve (`RES` roster status) are dropped
  entirely, not discounted.** Originally a 0.5x multiplier
  (`ROSTER_STATUS_MULTIPLIERS`), but that still left season-ending-injury
  players (e.g. Tyreek Hill's 2025 knee injury) sitting in the cheat sheet
  at a deflated-but-still-visible value, implying they're draftable in a
  redraft league when they aren't. Now `EXCLUDED_ROSTER_STATUSES` in
  `config.py` filters them out of `projections.csv` entirely — but only if
  that status held for `MIN_RES_STREAK_WEEKS`+ consecutive weeks (see
  `config.py`). A single most-recent-week check wrongly dropped Rashee
  Rice, whose last 2025 status was `RES` for just the final 2 weeks (a
  late-season shutdown), which looked identical to Tyreek Hill's real
  13-week season-ending injury under a single-week check.
- **Currently-rostered filter**: the model was including retired/
  unsigned/out-of-league players until a fix filtered the pool down to
  `load_rosters()` for the current season. This depends on nflreadpy's
  roster snapshot being up to date — worth rerunning close to draft day
  since free-agent signings can happen right up until then.
- **Tiers use a percentage-of-points gap threshold (`TIER_GAP_PCT_THRESHOLD`
  in config.py), not one global std-dev threshold across the whole
  candidate pool.** The std-dev approach produced badly lopsided tiers —
  a few huge gaps among the top 3-5 players at a position inflated the
  std dev enough that 30+ players in the WR2/WR3 range (each meaningfully
  different, individually 2-6% apart) never cleared the threshold and got
  dumped into one mega-tier. A percentage-of-points threshold self-scales
  instead.
- **No real signal distinguishes "missed games from a served suspension"
  from "missed games from injury"** in the games-played projection —
  known gap, not fixed. Rashee Rice missed 6 games to an NFL suspension
  in 2025 (already served, no bearing on 2026) and the model's crude
  games-played-per-season proxy penalizes it exactly like his 2024 ACL-
  related absence, dragging his projected games down to ~9 of 17 despite
  an elite ~17 ppg rate. Investigated two ways to distinguish them and
  both dead-ended: (1) `load_rosters_weekly()`'s `status_description_abbr`
  sub-codes (R01/R30/R40/etc.) aren't documented anywhere in nflverse's
  own dictionaries, and are empirically inconsistent — Calvin Ridley's
  real suspension shows `R30`, Deshaun Watson's shows `R40` in one season
  and `R01`/`R04` in others (his actual injury-driven reserve stints); (2)
  cross-referencing `load_injuries()` for a "not injury related"
  designation doesn't work either — that table is simply empty during a
  suspension (same blind spot as the original load_injuries() problem
  above: no entries generated when a player isn't practicing with the
  team, whether that's IR or suspension). If revisiting, a manual
  override list for specific known player-seasons is the fallback that
  doesn't require guessing at undocumented codes.

## Build order / status

Requirements → projection model (**done, backtested + grid-search
tuned, now covers QB/RB/WR/TE/K/DEF** — see "Kicker + Defense" above) →
cheat sheet (VBD/tiers, **done**) → React UI (**done, functional,
includes manual pick tracking, K/DEF tabs**) → live draft assistant
(**manual pick tracking done, including which picks are yours; Robust RB
strategy recommendation done, including a forced DEF/K recommendation
for your last 2 picks if you don't have one yet** — see "Draft Strategy
Recommendations" and "Kicker + Defense" below; positional scarcity
alerts beyond Robust RB not started) → ESPN auto-sync (**tabled until
after this season's draft** — see "ESPN Live Draft Sync — Design Notes"
below).

## Draft Strategy Recommendations (Robust RB v1)

Built in `fantasy-draft-app/src/strategies.js` + wired into `App.jsx`.
Extends manual pick tracking with a second, separate action per row: the
existing checkbox still means "drafted by someone else" (just removes
from the pool); a new star button means "drafted by me" (removes from
the pool AND adds to your roster — one click for the common case, since
most picks in a 14-team draft aren't yours).

- **Roster state**: `myRosterIds` (a second localStorage-persisted Set,
  separate from `draftedIds`) tracks your own picks specifically.
  `computeRosterCounts()` derives your position counts from it.
- **Current round**: derived from total picks made so far
  (`Math.floor(draftedIds.size / 14) + 1`) — no separate round input to
  keep in sync.
- **Robust RB scoring**: `TARGET_RB_COUNT=3`, `RB_BOOST_MULTIPLIER=1.35`,
  `BOOST_TAPER_START_ROUND=6` (all tunable in `strategies.js`) — boosts
  RB's VBD until your roster hits the target, tapering the boost off
  after round 6 so it doesn't force a late-round reach.
- **`MAX_ROSTER_COUNTS`** (QB:3, RB:6, WR:6, TE:3, also in
  `strategies.js`): a separate, strategy-agnostic soft cap — stops
  recommending a position once your roster is already deep at it,
  regardless of which strategy is active. Not roster-slot-exact (FLEX/
  bench blur the line), deliberately generous.
- **Strategies are registered in a `STRATEGIES` lookup** keyed by id →
  `{ label, score }`, so the UI's strategy `<select>` and future
  strategies (Zero RB, Hero RB, Balanced) just add an entry — no
  restructuring needed. Only Robust RB is registered today.
- **Recommended Pick callout**: shows the top-scoring available player
  under the active strategy (excluding positions already at
  `MAX_ROSTER_COUNTS`). If the strategy pick differs from the top player
  by raw VBD, shows both ("also consider: ...") so the BPA option stays
  visible alongside the strategy nudge.
- **Sort-by-strategy toggle**: re-sorts the ALL tab by strategy score
  instead of raw VBD when checked. Disabled on position tabs — those
  stay sorted by `position_rank` regardless, since tiers are a
  position-scoped concept the toggle deliberately doesn't touch.
- Verified with a scripted Playwright pass: mine-vs-theirs tracking,
  roster-count-driven target-met fallback (boost turns off exactly at
  3 RBs), sort-toggle reordering while the boost is active, tab-scoping,
  localStorage persistence across reload, and Reset clearing both
  `draftedIds` and `myRosterIds`. No console errors.

**Position guardrails tightened per user direction** (after seeing the
model draft too many TEs): `MAX_ROSTER_COUNTS` QB/TE caps dropped from
3 to 2; `POSITION_FLOORS` in `strategies.js` now also boosts WR toward a
floor of 3 (`TARGET_WR_COUNT`-equivalent), mirroring RB's existing
target-count-3 / 1.35x-multiplier treatment exactly — same magnitude by
default since no separate value was specified, independently tunable.
RB's floor (3) and cap (6) are unchanged. Verified: (1) the full
sort-by-strategy ordering matches a hand-computed replica of the scoring
formula exactly for the top 50 players from a fresh state, confirming
WR now gets boosted the same way RB does; (2) with 2 QB + 2 TE already
on the roster and every RB/WR drafted away (only QB/TE left in the
pool), the recommendation correctly falls back to "No recommendation
available" instead of suggesting a 3rd QB or TE — direct proof the cap
excludes them rather than just happening to lose to RB/WR on score.

**Real bug found and fixed after that**: the verification above only
proved the cap works for the single "Recommended:" callout — it never
tested the "Sort by strategy" toggle path. The user's actual drafting
method (confirmed directly: "sort by strategy and picking the top of
the list") bypassed the cap entirely, since `rows`' sort branch called
`compareByScoreThenVBD` directly with no `isPositionFull` check at all.
Concretely: QB/TE get no boost either way (score = raw VBD), so once
good RB/WR are gone in the late rounds and everything left is
negative-VBD, a capped-out position can still have a *less negative*
VBD than what's left elsewhere and float back to the top of "best
remaining" — this is exactly how a real run produced 3 QBs and 7 TEs
(one screenshot shared directly: `BENCH 3`–`BENCH 8` all TE, VBD as low
as -55.1) despite the caps supposedly being 2 each. Fixed in `App.jsx`'s
`rows` useMemo: when `sortByStrategy` is on, capped positions now sort
below every eligible player first, then by score/VBD within each group
— same effective exclusion the callout already had, just applied to the
full board ordering too. Re-verified with a full 14-round simulation
(autodraft on for opponents, sort-by-strategy on, picking top-of-list
for all 14 of my own picks, matching the reported workflow exactly):
final roster QB=2, TE=2, RB=4, WR=6 — caps and floors both hold for the
whole draft, not just the first few picks.

### Autodraft toggle (testing) + My Roster tab

Two more additions on top of the above, in `App.jsx`.

- **Autodraft toggle**: a checkbox next to the strategy selector, off by
  default, no effect on existing behavior when off. When on, each ★
  ("mine") pick is followed by 13 auto-picks (one full round of
  opponents — hardcoded to the 14-team league size, not a true
  snake-position simulator) added straight to `draftedIds` (never
  `myRosterIds`), each the highest-raw-VBD player still available, no
  strategy scoring involved. Implemented inside `handleDraftMine`'s
  `setDraftedIds` updater so all 14 changes land in one state update /
  one re-render. Still respects `MAX_ROSTER_COUNTS` from `strategies.js`
  via a **fresh per-batch position tally** (not your own roster counts)
  so one round can't pile up e.g. 10 QBs in a row; stops early
  (gracefully, no error) if the pool runs out or every remaining
  position is capped for that batch. Verified via Playwright, including
  both edge cases directly (small-pool exhaustion mid-batch, and a
  same-position-only pool where the cap — not pool exhaustion — is what
  stops the batch early).
- **My Roster tab**: added as a 6th tab (`ALL/QB/RB/WR/TE/MY ROSTER`)
  rather than a separate panel, reusing the existing tab mechanism. Shows
  your ★ picks in a greedy display-only slot fill (`buildRosterSlots()`):
  best QB, best 2 RB, best 2 WR, best TE, best remaining RB/WR/TE for
  FLEX, everything else to bench (padded to 7 slots, or more if you
  somehow draft beyond that — not silently dropped), plus an always-empty
  IR row. DEF/K aren't in the data at all so they're omitted entirely,
  not shown as permanently-empty rows. Empty slots render as "— empty —"
  rather than being omitted, since seeing what's still needed mid-draft
  is the point. The "Sort by strategy" toggle is disabled on this tab
  (same as the position tabs) since it only ever affected the ALL tab.
  Skipped the optional summary-line nice-to-have (total starter
  points) — the design doc flagged it as droppable if it added
  complexity, and the slot list alone covers the actual ask.
- **Autodraft randomness** (`AUTODRAFT_ALT_PICK_CHANCE = 0.25`, tunable):
  each of the 13 auto-picks usually takes the top-remaining-VBD
  candidate but sometimes (25%) takes the next-best instead, so all 13
  opponents don't read as one uniform BPA bot. **Real bug found and
  fixed while building this**: the random logic originally lived inside
  the `setDraftedIds` updater function passed to `setState`. That's an
  impurity violation — `App.jsx` renders inside `React.StrictMode`
  (`main.jsx`), which double-invokes updater functions in dev
  specifically to catch this, so `Math.random()` was silently getting
  called twice per pick, and the two invocations could disagree on
  which player got drafted. Fixed by computing the whole batch (mine
  pick + 13 auto-picks) up front in the `handleDraftMine` click handler
  from closure state, then passing a plain `Set` value to
  `setDraftedIds` instead of an updater function — a plain click
  handler isn't re-invoked by StrictMode, so this is safe. Verified by
  forcing `Math.random()` to a scripted sequence and hand-simulating
  the expected output in the test — exact match confirmed post-fix
  (mismatched pre-fix, confirming the bug was real, not just
  theoretical).

### Draft grade

A letter grade (D through A+) shown on the My Roster tab, unlocked once
your roster hits `FULL_ROSTER_SIZE` (originally 14; now 16 since the
K/DEF work added DEF/K starting slots to `STARTING_SLOTS` — this
threshold is derived from that array's length rather than a separate
hardcoded number, so it moved automatically when K/DEF were added, no
manual sync needed). `computeDraftGrade()` in `App.jsx`:

- Your total: sum of `projected_fantasy_points` across your (★) roster.
- The comparison baseline: sum of `projected_fantasy_points` across
  every OTHER drafted player, divided evenly by `OTHER_TEAM_COUNT`
  (`TEAM_COUNT - 1` = 13). **This is a single average-opponent baseline,
  not a real per-team distribution** — the app only tracks "mine" vs.
  "drafted by someone else," not draft order or which specific opponent
  took which player, so there's no way to build 13 individual team
  totals to rank against. Discussed directly with you: the alternative
  (tracking real draft order to bucket opponent picks round-robin into
  13 simulated teams, enabling actual rank-based grading) was explicitly
  turned down in favor of this simpler approach — though note the
  "Start Draft" flow (below) DOES now track a real draft position and
  snake order for the opponent-simulation logic; it just isn't wired
  into the draft-grade baseline, which still treats all non-mine picks
  as one undifferentiated pool.
- Grade bands are percent-above/below that baseline (tunable in
  `GRADE_BANDS`): A+ ≥+15%, A ≥+5%, B ≥-5%, C ≥-15%, D below that.
- Verified by seeding localStorage directly (top-14-by-projection as
  "mine" vs. the next 182 as "others", and the inverse) and confirming
  the app's displayed total/average/percent/grade match a hand
  computation exactly, plus the locked pre-14-players state and the
  panel's color coding (green A/A+, amber B, red C/D). No console
  errors.

### Small UI fixes (undocumented at the time, noted here after the fact)

- **"Recommended:" no longer shifts for a long player name**: Strategy
  select + Autodraft toggle moved to their own row, separate from the
  recommended-pick row (`.recommendation-panel__controls` /
  `.recommendation-panel__pick`), so a long name can't make the pick
  block too wide to fit next to them and push the whole line down as a
  unit. The label/chip/name group also got wrapped in its own
  `flex-wrap: nowrap` container (`.recommendation-panel__pick-main`) so
  they can never break apart from each other — only the trailing "also
  consider" / forced note is still allowed to wrap onto its own line.
- **Roster locks at `FULL_ROSTER_SIZE`**: once `myRosterIds.size >=
  FULL_ROSTER_SIZE`, the ★ button disables (both in the UI and inside
  `handleDraftMine` itself) until Reset, and the recommendation panel
  shows "Your roster is full — reset to draft more" instead of a pick.
  Only the ★ ("mine") action is blocked — clicking a name to mark
  someone else's pick still works normally throughout, since that
  tracking needs to continue for the rest of the draft regardless of
  your own roster status.

### Start Draft flow (snake-accurate opponent simulation)

Replaces "autodraft only ever fires after your ★ pick, always simulating
a flat 13" with a real draft-start flow: enter your draft position
(1-`TEAM_COUNT`), click **Start Draft**, and picks that would've
happened before your turn get filled in automatically.

- **New state**: `draftStarted` (bool) and `myPickPosition`
  (int 1-14 or `null`), both persisted to localStorage (new keys
  `fantasydrafter:draftStarted` / `fantasydrafter:myPickPosition`) so a
  reload mid-draft doesn't drop back into the "enter your position"
  state. Both reset via `handleReset`, same as `draftedIds`/`myRosterIds`.
- **Board is inert until Start Draft is clicked**: both the ★ button and
  the plain name-click ("drafted by someone else") are disabled —
  previously only the ★ button had a disabled state (for the roster-full
  case); this extends the same pattern to the name-click for the
  pre-draft-start case specifically. A prompt below the controls row
  ("Enter your pick number and click Start Draft to begin.") explains why.
- **Start Draft pre-fill**: `picksBeforeMe = myPickPosition - 1` opponent
  picks get simulated immediately via the shared helper (see below) —
  this happens **unconditionally**, regardless of the "Autodraft
  (testing)" toggle. These aren't optional test picks; they're a
  structural necessity (the board can't sensibly show you "on the clock"
  for pick 5 with picks 1-4 not existing).
- **Ongoing rounds now use real snake math** instead of a flat 13:
  `computeSnakePickNumber(round, myPickPosition, teamCount)` and
  `computeOpponentGap(round, myPickPosition, teamCount)` in
  `strategies.js` implement the standard snake formula (odd rounds
  1..N, even rounds N..1) and derive the gap between your pick in round
  R and round R+1. This gap-fill step (unlike the initial pre-fill)
  **remains gated by the Autodraft toggle** — it's still a testing
  convenience for the ongoing draft, not a structural requirement, since
  you can always mark opponent picks by hand instead.
- **Refactor**: the inline "simulate N picks" logic that used to live
  directly in `handleDraftMine` (hardcoded around the old flat
  `OPPONENT_PICKS_PER_ROUND = 13`) was extracted into a standalone
  `simulateOpponentPicks(count, currentDraftedIds, players)` in
  `App.jsx`, called by both Start Draft (variable count) and the ongoing
  per-round autodraft loop (now a computed count via
  `computeOpponentGap`). `OPPONENT_PICKS_PER_ROUND` no longer exists as
  a constant — count is always a per-call computed value now. Preserved
  the existing synchronous-closure-before-`setDraftedIds` pattern (the
  `Math.random()` + `React.StrictMode` double-invocation bug fixed
  earlier — see the autodraft-randomness entry above — would have
  reappeared here if this had been written as an updater function).
- **Real bug found and fixed during this work**: `simulateOpponentPicks`'s
  position-cap logic (`MAX_ROSTER_COUNTS`, meant to stop one simulated
  batch from piling up e.g. 10 QBs in a row) was designed under the
  assumption that `count` ≈ 13 (one round = 13 different opponents each
  picking once) — a cap of "one team's max at a position" made sense
  against that. But a snake gap can be as large as 26 (two full rounds'
  worth, for a draft position near either extreme), and capping a
  26-pick call at a single team's limits (QB:2+RB:6+WR:6+TE:2+K:1+DEF:1
  = 18 total) cut the batch off after ~18 picks, silently under-filling
  by 8. Fixed by scaling the cap by how many "rounds" the count spans
  (`roundsSpanned = Math.ceil(count / (TEAM_COUNT - 1))`, capped
  multiplied by that) — unchanged behavior for the common ~13-pick case
  (`roundsSpanned` stays 1), correctly larger caps for bigger multi-round
  gaps. Also had to fix an unrelated naming collision while removing the
  old constant: `computeDraftGrade`'s "divide by 13 other teams"
  baseline had been reusing `OPPONENT_PICKS_PER_ROUND` for a completely
  different concept (team count, not pick count) — split into its own
  `OTHER_TEAM_COUNT = TEAM_COUNT - 1`.
- **Validation**: the pick-position `<input>` clamps to 1-`TEAM_COUNT` on
  every change (typing 20 snaps to 14, 0 snaps to 1) rather than just
  rejecting on submit; Start Draft is disabled whenever the value isn't
  valid, as a second layer on top of the clamping.
- **Verified exhaustively** (this was flagged as the highest-risk new
  logic, worth dedicated test cases): position 1 (round1→2 gap=26,
  round2→3 gap=0), position 14 (inverse), and position 7 (middle) all
  checked against hand-derived expected totals using the correct
  formula — `total drafted after your round-N pick = pickNumber(N+1,
  myPickPosition) - 1`, since the gap-fill for the NEXT round happens
  immediately as part of the same click that completes round N (a
  subtlety that tripped up the first draft of the test itself before
  landing on the right formula). All matched exactly across 4+ rounds
  per position. Also reconfirmed the position-cap regression check
  (a 13-pick pre-fill still respects QB/TE ≤ their caps) and the
  board-inert / Reset-clears-position / out-of-range-clamping behaviors.

## Where things stand (session handoff)

Everything below is committed and pushed to `main` — working tree clean,
nothing local-only except gitignored secrets (see the ESPN section).

- **Model**: DECAY, TEAM_OFFENSE_ADJUSTMENT_STRENGTH, and
  TARGET_SHARE_ADJUSTMENT_STRENGTH are all backtest-confirmed (see
  "Tuning" section above). QB_RUSHING_YARDS_WEIGHT no longer exists —
  removed as part of the Goff fix (see "Projection accuracy diagnostic +
  fixes" below); QB_RUSHING_EMPHASIS_STRENGTH re-tuned to 0.05 (was 0.3)
  for the reformulated (rushing-only) signal. AGE_CURVES: RB reverted to
  the original domain-knowledge value and backtest-validated (real
  overfitting evidence, not just theoretical caution — see that same
  section); WR and TE remain at the grid-search values, still flagged
  low-confidence, don't trust a future grid_search.py Stage 3 run just
  because it looks "consistent." AGE_TREND_DAMPENING re-checked and
  found genuinely unstable against different curve combinations — kept
  at its prior value (0.1) rather than chase noise. LOOKBACK_SEASONS
  left at its original default (5) — the search signal for it was noisy
  across windows, not worth chasing.
- **App**: manual pick tracking works — checkbox removes a player from
  the board, Reset (with a confirm dialog) brings everyone back,
  localStorage-persisted so a reload mid-draft doesn't lose picks.
  Verified with a scripted Playwright pass (screenshots + console-error
  check), not just by reading the code.
- **ESPN auto-sync**: tested twice now, against two separate mock
  drafts, with two independent libraries — same negative result both
  times. (1) Python `espn_api`: `_fetch_draft()` explicitly won't
  populate any picks until ESPN's own `drafted` flag is `true` (whole
  draft complete). (2) JS `espn-fantasy-football-api`'s
  `getDraftInfo({ seasonId })`: this one has NO such gate — it returns
  the full draft grid immediately (224 slots for a 14-team/16-round
  league), with unfilled slots as placeholders (`id: -1`). Made a real
  pick in the mock draft UI and polled directly for 20+ seconds
  afterward: still 0 filled slots. Because this second test bypassed
  any library-side "wait until complete" gate and still saw nothing
  live, this is now decent evidence the live data genuinely isn't there
  for a **mock** draft specifically — not just a library limitation.
  **Important caveat, still unconfirmed**: both tests were against mock
  drafts. Mock drafts may be simulated client-side and never hit
  whatever backend a real paid-league draft writes to — this result may
  not generalize to a real draft. If revisiting after this season, test
  against a real league draft before concluding Option A is fully dead;
  otherwise fall back to Option B (Playwright reading the draft room
  DOM) — see the design notes below, now updated with these findings.
  `tests/espn_draft_sync_spike.py` (Python, committed in the repo) is
  the polling script already built for this. The JS package/scratch
  folder from the second test was throwaway (outside the repo, in the
  session scratchpad) and has been fully deleted — nothing to clean up
  there. `tests/espn_credentials.local.json` (gitignored, NOT in git)
  holds real SWID/espn_s2 cookies if that file is still on this machine
  — no need to re-extract them from the browser to pick this back up.
  **Open question left unanswered**: whether to `pip uninstall espn_api`
  from `.venv` (unused by any production code, only by the spike
  script, not declared in any requirements file) — asked, no response
  yet, harmless to leave either way.
- **Before your actual draft**: rerun `./refresh_cheat_sheet.sh` close
  to draft day — the currently-rostered filter depends on nflreadpy's
  roster snapshot being current, and free-agent signings/depth-chart
  moves can happen right up until then.
- **Publishing**: `npm run build && npm run deploy` from
  `fantasy-draft-app/` publishes to GitHub Pages (already configured —
  correct `base` path, `gh-pages` package installed). Not run by Claude
  this session — you said you'd handle the actual Pages publish
  yourself, so confirm it's live before relying on the hosted URL during
  a draft.

# ESPN Live Draft Sync — Design Notes

Goal: when you make a pick in the actual ESPN draft room, the app updates
automatically — no manual "mark as picked" step.

## What's actually available

ESPN has no official public API for this. Everything below is
unofficial/reverse-engineered, used at your own risk (could break without
notice if ESPN changes something).

### Option A — Poll ESPN's private REST API

ESPN's site itself calls an undocumented endpoint to load draft data:

```
GET https://fantasy.espn.com/apis/v3/games/ffl/seasons/{season}/segments/0/leagues/{league_id}?view=mDraftDetail
```

- For a **private league** (yours), this requires two auth cookies from
  your own browser session: `SWID` and `espn_s2`. You grab these once
  from your browser's dev tools (Application → Cookies) after logging
  into ESPN, and the script sends them as request cookies.
- There's an established Python wrapper, `espn-api` (cwendt94 on GitHub,
  `pip install espn_api`), that handles this — `league.draft` returns
  pick objects (player, round, team) after calling `.fetch()` /
  re-initializing the `League` object. There's also a JS wrapper,
  `espn-fantasy-football-api` (mkreiser on GitHub) —
  `client.getDraftInfo({ seasonId })` returns the full draft grid.
- **Tested twice (see "Where things stand" above for full detail) —
  does NOT reflect live picks for a mock draft, with either library.**
  The Python wrapper gates on ESPN's `drafted` flag (won't show
  anything until the whole draft is marked complete). The JS wrapper
  has no such gate and returns real-time data structurally (a
  pre-sized grid of pick slots, `id: -1` for unfilled ones) — and it
  still showed 0 filled slots 20+ seconds after a real pick was made in
  the UI. The open GitHub issue that originally raised this question
  (cwendt94/espn-api#558) is still unanswered upstream, but our own
  test is a fairly direct answer for the mock-draft case.
- **Still open**: whether a REAL league draft (not a mock) behaves the
  same way. Mock drafts may be simulated client-side and never hit
  whatever backend a real draft writes to — untested, and the natural
  next step if revisiting this after the mock-draft evidence.

### Option B — Read the live draft room page directly

This is what the commercial tools (DraftKick, Draft Sharks Game Changer)
appear to actually do — they run as a browser extension inside the ESPN
draft room tab itself, reading whatever's rendered on screen, rather than
calling ESPN's backend API.

- More resilient to backend API changes (reads the UI, not the private
  API) — but breaks instead if ESPN changes their page's HTML structure.
- Two ways to implement this:
  1. **A real Chrome extension** — most robust, but by far the most
     engineering effort (extension packaging, content scripts, dev
     workflow).
  2. **A Playwright script** that loads the draft room with your saved
     login cookies and polls the rendered DOM on an interval, diffing
     for new picks. Simpler than a full extension, still works while a
     browser window stays open during your draft. (There's a public
     example of someone doing roughly this for a similar "read the
     draft room and feed it to an LLM" use case — scraping player pool
     HTML with Playwright + saved cookies.)
- Only works while that browser window/tab stays open and on the draft
  page for the duration of your draft.

### Option C — Manual entry (the fallback, already effectively built)

Worth keeping regardless of which auto-sync option is pursued — if the
live sync lags, drops a pick, or breaks mid-draft, you need a way to
correct it by hand without the whole tool being useless for the rest of
the draft.

## Recommendation

**Current status: tabled until after this season's draft** — manual
entry (Option C) is built and in use (the checkbox + Reset UI in the
React app). The plan below is for if/when this gets revisited.

1. ~~Test Option A first~~ — **done, twice, negative result for mock
   drafts** (see above). Before spending more time on Option A, test
   against a REAL league draft specifically — the mock-draft result may
   not generalize, and this is a five-minute check before committing to
   Option B's much larger effort.
2. **If Option A still doesn't reflect live picks on a real draft**,
   move to Option B, starting with the Playwright DOM-scraping approach
   (2b) rather than a full Chrome extension (2a) — much less effort, and
   sufficient for a personal tool used once a year.
3. **Always keep manual override/correction available** (Option C),
   regardless of which auto path is used — auto-sync should be a
   convenience layer on top of a system that still works if it fails.

## Auth handling note

`SWID` and `espn_s2` are session auth cookies, not your ESPN password —
but they're still sensitive (they grant access to your account's private
league data) and shouldn't be committed to the GitHub repo. Store them in
a local `.env` file or similar, and make sure it's covered by
`.gitignore` (same mistake as the `.venv` commit earlier — worth
double-checking before the first commit that touches this).