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

## Known gotchas already hit (don't re-debug these)

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