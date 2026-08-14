# fantasydrafter
# Fantasy Football Draft Tool — Requirements

## 1. Overview
A Python tool built on `nflreadpy` to support fantasy football drafting in two modes:
- **Pre-draft**: generate a ranked cheat sheet / tiers before draft day.
- **Live draft assistant**: track picks in real time and surface recommendations as the draft unfolds.

**League settings (confirmed):**
- Scoring: Full PPR
- Draft format: Snake

**League settings (default values, user-editable in the app):**
- League size: 14 teams (editable)
- Roster slots: 1 QB, 2 RB, 1 TE, 2 WR, 1 FLEX, 1 DEF, 1 K + 7 bench + 1 IR (editable)
- Redraft vs. keeper/dynasty: Redraft (fresh draft each year, no keepers/rookie carryover)

---

## 2. Data Requirements

### 2a. Available directly from `nflreadpy`
- Rosters & player IDs (`load_rosters`)
- Weekly/seasonal player stats (`load_player_stats`) — can be used to compute *historical* fantasy points under your Full PPR scoring
- Schedules (`load_schedules`) — for bye weeks and strength-of-schedule
- Snap counts (`load_snap_counts`) — usage trends, workload share
- Injuries (`load_injuries`)
- Next Gen Stats (`load_nextgen_stats`) — advanced efficiency metrics (separation, air yards, etc.)
- Combine / draft pick data — useful mainly for rookie evaluation
- Depth charts (`load_depth_charts`) — starter vs. backup role

### 2b. NOT available from `nflreadpy` — need another source or your own model
- **Fantasy point projections for the upcoming season** (nflreadpy only has historical/actual stats, not forward-looking projections)
- **Average Draft Position (ADP)** — needed to flag value picks / reaches
- **Expert consensus rankings (ECR)** — optional, but common cheat-sheet input

**Decision: build an in-house projection/ADP model from `nflreadpy` historical data**, using as much historical data as available (weighted, e.g. more recent seasons weighted higher, regressed for age/injury/role change). ADP itself (what other drafters value) is harder to model from history alone since it reflects market sentiment, not just performance — may need a proxy (e.g., prior-year end-of-season rank) rather than a true ADP.

---

## 3. Functional Requirements — Pre-Draft Cheat Sheet
- [ ] Compute Full PPR fantasy points from historical stats, by player/position/season
- [ ] Blend in projections/ADP from external source (per 2b)
- [ ] Rank players overall and by position
- [ ] Group into tiers (e.g., statistical clustering or manual cutoffs) to show "drop-off" points
- [ ] Value-Based Drafting (VBD) score — value over baseline replacement player at each position
- [ ] Flag risk factors: injury history, target/snap share volatility, age/decline curve, new team/scheme
- [ ] Bye week list per player, with conflict warnings (too many players on the same bye)
- [ ] Exportable cheat sheet (CSV/printable) sorted by rank or by position

## 4. Functional Requirements — Live Draft Assistant
- [ ] Input/track picks as they happen (manual entry or draft platform integration if available)
- [ ] Maintain live "available players" pool (cheat sheet minus drafted players)
- [ ] Track your roster and remaining roster needs (e.g., "still need 1 more WR, 1 FLEX")
- [ ] "Best player available" recommendation, adjustable by:
  - pure value (VBD/rank)
  - positional need
  - roster construction rules
- [ ] Positional scarcity alerts (e.g., "only 3 startable TEs left")
- [ ] Reach/value flags (comparing current pick to ADP)
- [ ] Turn timer awareness / pick countdown (nice-to-have)

---

## 5. Remaining Open Questions
1. **ESPN auto-sync** — deferred to last: build the tool with manual pick entry first, layer in ESPN sync as the final piece once the core rankings/draft-assistant logic works. (No official public ESPN draft API exists, so this will likely rely on unofficial endpoints and, for private leagues, session cookies — worth revisiting feasibility when we get there.)

---

## 6. Technical Requirements
- **Backend/data processing**: Python, `nflreadpy` for NFL data, projection model built in-house
- **Frontend**: React-based site
- **Hosting**: GitHub Pages (free) — note: GitHub Pages is static hosting only, so any live logic (draft sync, roster tracking) needs to run client-side (in the browser) or via a separate free backend (e.g., precomputed static JSON for pre-draft rankings, with draft-tracking state kept in-browser). ESPN auto-sync (Section 5) will need its own solution since it can't run from GitHub Pages directly — to be resolved when tackled.
- **Data refresh cadence**: on-demand regeneration of rankings/projections before draft day (e.g., re-run a Python script, export to JSON consumed by the React app); live pick updates during the draft via ESPN sync (final feature) or manual entry (default/fallback)
- **Storage**: no server-side database needed — precomputed JSON/static data for rankings; in-browser state (or local storage) for live draft tracking
- **Historical data window**: weight as many years as `nflreadpy` reliably provides; if a cutoff is needed for data quality/model complexity, default to the last 20 NFL seasons as a representative long-run average, with recency weighting on top
