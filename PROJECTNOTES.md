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

Requirements → projection model → cheat sheet (VBD/tiers) → **React UI
(done, functional)** → **live draft assistant (not started)** → ESPN
auto-sync (explicitly last, deferred — no official public API, will need
unofficial endpoints and possibly session cookies for private leagues).

Next planned step: the live draft assistant — track picks (manual entry
first), maintain available-player pool, track roster needs, surface
best-available recommendations, positional scarcity alerts.