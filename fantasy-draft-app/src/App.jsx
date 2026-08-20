import { useEffect, useMemo, useState } from "react";
import {
  STRATEGIES,
  DEFAULT_STRATEGY,
  MAX_ROSTER_COUNTS,
  TEAM_COUNT,
  computeCurrentRound,
  computeOpponentGap,
  computeRosterCounts,
  isPositionFull,
  compareByScoreThenVBD,
  crossPositionValue,
} from "./strategies";

const POSITIONS = ["QB", "RB", "WR", "TE", "K", "DEF"];
const ROSTER_SLOT_TAB = "MY ROSTER";
const TABS = ["ALL", ...POSITIONS, ROSTER_SLOT_TAB];

// League size minus you — used for the draft-grade "average other team"
// baseline. Distinct concept from the opponent-pick simulation below
// (that one varies per round now, via computeOpponentGap; this one is
// just "how many other teams are there," always fixed).
const OTHER_TEAM_COUNT = TEAM_COUNT - 1;

// Tunable — how often a simulated opponent pick takes the next-best-VBD
// player instead of the top one, so simulated opponents don't all read
// as one uniform BPA bot.
const AUTODRAFT_ALT_PICK_CHANCE = 0.25;

const STARTING_SLOTS = [
  { position: "QB", label: "QB" },
  { position: "RB", label: "RB1" },
  { position: "RB", label: "RB2" },
  { position: "WR", label: "WR1" },
  { position: "WR", label: "WR2" },
  { position: "TE", label: "TE" },
  { position: "DEF", label: "DEF" },
  { position: "K", label: "K" },
];
const FLEX_ELIGIBLE = ["RB", "WR", "TE"];
const BENCH_SLOT_COUNT = 7;
// 8 starting slots (incl. DEF/K) + FLEX + 7 bench = 16 — a full roster
// under this app's slot model (not counting IR, which has no fill logic).
// Draft grade unlocks once your roster reaches this size, deliberately
// derived from the slot model above rather than a separate hardcoded
// number, so the two can't drift out of sync if the roster shape ever
// changes.
const FULL_ROSTER_SIZE = STARTING_SLOTS.length + 1 + BENCH_SLOT_COUNT;

// Percent above/below the average other team's tallied projected points.
// Tunable — bands are deliberately symmetric around a +/-5% "about average"
// middle band.
const GRADE_BANDS = [
  { min: 15, grade: "A+" },
  { min: 5, grade: "A" },
  { min: -5, grade: "B" },
  { min: -15, grade: "C" },
  { min: -Infinity, grade: "D" },
];

// Persisted across reloads — a live draft can run long, and losing every
// checked-off pick to an accidental refresh would be a real problem, not
// just an inconvenience.
const DRAFTED_STORAGE_KEY = "fantasydrafter:draftedPlayerIds";
const MY_ROSTER_STORAGE_KEY = "fantasydrafter:myRosterPlayerIds";
const DRAFT_STARTED_STORAGE_KEY = "fantasydrafter:draftStarted";
const MY_PICK_POSITION_STORAGE_KEY = "fantasydrafter:myPickPosition";

function loadIdSet(key) {
  try {
    const raw = localStorage.getItem(key);
    return raw ? new Set(JSON.parse(raw)) : new Set();
  } catch {
    return new Set();
  }
}

function loadBoolean(key) {
  return localStorage.getItem(key) === "true";
}

function loadPickPosition() {
  const raw = localStorage.getItem(MY_PICK_POSITION_STORAGE_KEY);
  const parsed = raw === null ? null : Number(raw);
  return parsed && parsed >= 1 && parsed <= TEAM_COUNT ? parsed : null;
}

function ValueBadge({ delta }) {
  // delta = vbd_vs_adp_proxy_overall: positive means the model likes this
  // player earlier than their prior-season finish (proxy) would suggest;
  // negative means the opposite.
  if (delta === null || delta === undefined || Number.isNaN(delta)) {
    return <span className="value-badge value-badge--neutral">—</span>;
  }
  if (delta > 0) {
    return <span className="value-badge value-badge--up">▲ {delta}</span>;
  }
  if (delta < 0) {
    return <span className="value-badge value-badge--down">▼ {Math.abs(delta)}</span>;
  }
  return <span className="value-badge value-badge--neutral">— 0</span>;
}

function PositionChip({ position }) {
  return <span className={`chip chip--${position}`}>{position}</span>;
}

function PlayerRow({ player, rankField, onDraft, onDraftMine, myRosterFull, draftStarted }) {
  const mineDisabled = myRosterFull || !draftStarted;
  const mineTitle = !draftStarted
    ? "Enter your pick number and click Start Draft first"
    : myRosterFull
    ? "Your roster is full — reset to draft more"
    : "Drafted by your team";

  return (
    <tr>
      <td className="col-mine">
        <button
          type="button"
          className="mine-button"
          aria-label={
            mineDisabled
              ? mineTitle
              : `Mark ${player.player_display_name} as drafted by your team`
          }
          title={mineTitle}
          disabled={mineDisabled}
          onClick={() => onDraftMine(player.player_id)}
        >
          ★
        </button>
      </td>
      <td className="col-rank">{player[rankField]}</td>
      <td className="col-name">
        <button
          type="button"
          className="name-button"
          aria-label={`Mark ${player.player_display_name} as drafted`}
          title={!draftStarted ? "Enter your pick number and click Start Draft first" : "Drafted by someone else"}
          disabled={!draftStarted}
          onClick={() => onDraft(player.player_id)}
        >
          <PositionChip position={player.position} /> {player.player_display_name}
        </button>
      </td>
      <td className="col-num">{player.projected_fantasy_points.toFixed(1)}</td>
      <td className="col-num">{player.vbd.toFixed(1)}</td>
      <td className="col-value">
        <ValueBadge delta={player.vbd_vs_adp_proxy_overall} />
      </td>
    </tr>
  );
}

function RosterRow({ slotLabel, player }) {
  return (
    <tr className={player ? undefined : "roster-row--empty"}>
      <td className="col-slot">{slotLabel}</td>
      <td className="col-name">
        {player ? (
          <>
            <PositionChip position={player.position} /> {player.player_display_name}
          </>
        ) : (
          <span className="roster-row__empty-label">&mdash; empty &mdash;</span>
        )}
      </td>
      <td className="col-num">{player ? player.projected_fantasy_points.toFixed(1) : "—"}</td>
      <td className="col-num">{player ? player.vbd.toFixed(1) : "—"}</td>
    </tr>
  );
}

// Greedy display-only fill: best QB, best 2 RBs, best 2 WRs, best TE, best
// remaining RB/WR/TE for FLEX, everything else to bench. Not a "start your
// best lineup" optimizer (that's a weekly in-season concern) — just a way
// to see what's filled vs. still needed at a glance mid-draft. DEF/K aren't
// in the data at all, so they're omitted rather than shown as permanently
// empty placeholders.
function buildRosterSlots(players, myRosterIds) {
  const mine = players.filter((p) => myRosterIds.has(p.player_id));
  const used = new Set();

  const takeBest = (position) => {
    const best = mine
      .filter((p) => p.position === position && !used.has(p.player_id))
      .sort((a, b) => b.vbd - a.vbd)[0];
    if (best) used.add(best.player_id);
    return best ?? null;
  };

  const starterSlots = STARTING_SLOTS.map(({ position, label }) => ({
    label,
    player: takeBest(position),
  }));

  const flex = mine
    .filter((p) => FLEX_ELIGIBLE.includes(p.position) && !used.has(p.player_id))
    .sort((a, b) => b.vbd - a.vbd)[0];
  if (flex) used.add(flex.player_id);

  const bench = mine.filter((p) => !used.has(p.player_id)).sort((a, b) => b.vbd - a.vbd);

  return {
    slots: [...starterSlots, { label: "FLEX", player: flex ?? null }],
    bench,
    benchSlotCount: Math.max(BENCH_SLOT_COUNT, bench.length),
  };
}

// Grades your roster's tallied projected points against a single "average
// other team" baseline — the combined projected points of everyone drafted
// by someone else, divided evenly across the 13 other teams. Not a real
// per-team distribution (the app doesn't track draft order, only who
// drafted whom), so this is a vs-average comparison, not a literal league
// ranking.
function computeDraftGrade(players, draftedIds, myRosterIds) {
  const byId = new Map(players.map((p) => [p.player_id, p]));
  const sumProjected = (ids) => {
    let total = 0;
    for (const id of ids) {
      total += byId.get(id)?.projected_fantasy_points ?? 0;
    }
    return total;
  };

  const yourTotal = sumProjected(myRosterIds);
  const otherIds = [...draftedIds].filter((id) => !myRosterIds.has(id));
  const otherTotal = sumProjected(otherIds);
  const otherAvg = otherTotal / OTHER_TEAM_COUNT;

  // No signal to compare against yet (shouldn't normally happen once your
  // own roster is full, but guards the divide either way).
  if (otherAvg <= 0) {
    return { yourTotal, otherAvg, percentDiff: null, grade: "A+" };
  }

  const percentDiff = ((yourTotal - otherAvg) / otherAvg) * 100;
  const grade = GRADE_BANDS.find((band) => percentDiff >= band.min).grade;

  return { yourTotal, otherAvg, percentDiff, grade };
}

// Simulates `count` opponent picks on top of currentDraftedIds — used
// both for the Start Draft pre-fill (picks before your first turn,
// variable count) and the ongoing post-your-pick autodraft loop (still
// gated by the autodraft toggle, count now snake-accurate instead of a
// flat 13 — see computeOpponentGap in strategies.js). Each pick is the
// highest-VBD player still available, no strategy scoring, with a 25%
// chance of taking the next-best instead so simulated opponents don't
// all read as one uniform BPA bot. A fresh per-call position tally
// (not tied to any real roster) keeps one call from piling up e.g. 10
// QBs in a row; stops early if the pool runs out or every remaining
// position is capped for this call. Returns a NEW Set — never mutates
// currentDraftedIds.
//
// The position cap (MAX_ROSTER_COUNTS) represents ONE team's realistic
// max at a position — fine when count is ~13 (one round = 13 different
// opponents each picking once), but a snake-draft gap can span up to 26
// (two full rounds' worth, for a draft position near either end). Capping
// a 26-pick call at a single team's limits (e.g. RB:6) would cut the
// batch off after ~18 total picks, well short of 26, silently under-
// filling. Scaling the cap by how many "rounds" the count spans (1 round
// = TEAM_COUNT-1 opponent picks) keeps the original single-round
// behavior unchanged while correctly allowing a multi-round gap to use a
// proportionally larger cap.
function simulateOpponentPicks(count, currentDraftedIds, players) {
  const roundsSpanned = Math.max(1, Math.ceil(count / (TEAM_COUNT - 1)));
  const batchCap = (position) => (MAX_ROSTER_COUNTS[position] ?? Infinity) * roundsSpanned;

  const nextDrafted = new Set(currentDraftedIds);
  const batchCounts = { QB: 0, RB: 0, WR: 0, TE: 0 };
  for (let i = 0; i < count; i++) {
    const candidates = players
      .filter((p) => !nextDrafted.has(p.player_id))
      .filter((p) => (batchCounts[p.position] ?? 0) < batchCap(p.position))
      .sort((a, b) => b.vbd - a.vbd);
    if (candidates.length === 0) break; // pool exhausted or every position capped this batch

    const takeAlt = candidates.length > 1 && Math.random() < AUTODRAFT_ALT_PICK_CHANCE;
    const pick = takeAlt ? candidates[1] : candidates[0];

    nextDrafted.add(pick.player_id);
    batchCounts[pick.position] = (batchCounts[pick.position] ?? 0) + 1;
  }
  return nextDrafted;
}

function TierDivider({ tier }) {
  return (
    <tr className="tier-divider-row">
      <td colSpan={6}>
        <div className="tier-divider">
          <span className="tier-divider__chevron">▶</span>
          <span className="tier-divider__label">Tier {tier}</span>
          <span className="tier-divider__line" />
        </div>
      </td>
    </tr>
  );
}

export default function App() {
  const [players, setPlayers] = useState(null);
  const [error, setError] = useState(null);
  const [activeTab, setActiveTab] = useState("ALL");
  const [draftedIds, setDraftedIds] = useState(() => loadIdSet(DRAFTED_STORAGE_KEY));
  const [myRosterIds, setMyRosterIds] = useState(() => loadIdSet(MY_ROSTER_STORAGE_KEY));
  const [activeStrategy, setActiveStrategy] = useState(DEFAULT_STRATEGY);
  const [sortByStrategy, setSortByStrategy] = useState(false);
  const [autodraftEnabled, setAutodraftEnabled] = useState(false);
  const [draftStarted, setDraftStarted] = useState(() => loadBoolean(DRAFT_STARTED_STORAGE_KEY));
  const [myPickPosition, setMyPickPosition] = useState(loadPickPosition);

  useEffect(() => {
    // Fetched from public/data/cheat_sheet.json at runtime — regenerate
    // this file via scripts/csv_to_json.py before each build/deploy so
    // it reflects your latest cheat_sheet.csv output.
    fetch(`${import.meta.env.BASE_URL}data/cheat_sheet.json`)
      .then((res) => {
        if (!res.ok) throw new Error(`Failed to load data (${res.status})`);
        return res.json();
      })
      .then(setPlayers)
      .catch((err) => setError(err.message));
  }, []);

  useEffect(() => {
    localStorage.setItem(DRAFTED_STORAGE_KEY, JSON.stringify([...draftedIds]));
  }, [draftedIds]);

  useEffect(() => {
    localStorage.setItem(MY_ROSTER_STORAGE_KEY, JSON.stringify([...myRosterIds]));
  }, [myRosterIds]);

  useEffect(() => {
    localStorage.setItem(DRAFT_STARTED_STORAGE_KEY, String(draftStarted));
  }, [draftStarted]);

  useEffect(() => {
    if (myPickPosition === null) {
      localStorage.removeItem(MY_PICK_POSITION_STORAGE_KEY);
    } else {
      localStorage.setItem(MY_PICK_POSITION_STORAGE_KEY, String(myPickPosition));
    }
  }, [myPickPosition]);

  const isValidPickPosition =
    myPickPosition !== null && myPickPosition >= 1 && myPickPosition <= TEAM_COUNT;

  const handlePickPositionChange = (e) => {
    const raw = e.target.value;
    if (raw === "") {
      setMyPickPosition(null);
      return;
    }
    const parsed = Number(raw);
    if (Number.isNaN(parsed)) {
      setMyPickPosition(null);
      return;
    }
    setMyPickPosition(Math.min(TEAM_COUNT, Math.max(1, Math.round(parsed))));
  };

  // Fills in the picks that would've happened before your turn if you're
  // joining a draft at any position other than #1 — a structural
  // necessity (the board can't sensibly show you "on the clock" for pick
  // 5 with picks 1-4 not existing), not an optional test convenience, so
  // this runs regardless of the autodraft toggle.
  const handleStartDraft = () => {
    if (draftStarted || !players || !isValidPickPosition) return;
    const picksBeforeMe = myPickPosition - 1;
    setDraftedIds(simulateOpponentPicks(picksBeforeMe, draftedIds, players));
    setDraftStarted(true);
  };

  const handleDraft = (playerId) => {
    if (!draftStarted) return;
    setDraftedIds((prev) => new Set(prev).add(playerId));
  };

  // A pick you made yourself is both "off the board" and "on your roster" —
  // one click covers both, since most picks (13/14) are the plain "drafted
  // by someone else" case that only needs the former.
  //
  // Computed from closure state up front (not inside a setDraftedIds
  // updater) because autodraft's Math.random() makes this impure — an
  // updater function must be pure, since React.StrictMode double-invokes it
  // in dev to check exactly that, which would burn two random draws per
  // pick instead of one. A plain click handler isn't re-invoked that way,
  // so reading draftedIds from closure here is safe.
  const handleDraftMine = (playerId) => {
    // Belt-and-suspenders: the ★ button is already disabled before the
    // draft starts and once the roster is full (see myRosterFull below),
    // but guard the handler itself too rather than relying solely on the
    // UI being disabled.
    if (!draftStarted || myRosterIds.size >= FULL_ROSTER_SIZE) return;

    let nextDrafted = new Set(draftedIds).add(playerId);

    // Testing aid: fills in the rest of this round's opponent picks, so
    // you don't have to manually click through every one. The GAP is
    // snake-accurate (see computeOpponentGap) — round 1's pre-fill is
    // handled separately by Start Draft above; this covers every round
    // after that. Unlike the Start Draft pre-fill, this remains gated by
    // the toggle — it's a convenience for testing, not a structural
    // requirement (you can always mark opponent picks by hand instead).
    if (autodraftEnabled && players && isValidPickPosition) {
      const round = computeCurrentRound(draftedIds.size);
      const gap = computeOpponentGap(round, myPickPosition);
      nextDrafted = simulateOpponentPicks(gap, nextDrafted, players);
    }

    setDraftedIds(nextDrafted);
    setMyRosterIds((prev) => new Set(prev).add(playerId));
  };

  const handleReset = () => {
    if (draftedIds.size === 0) return;
    if (!window.confirm(`Reset ${draftedIds.size} drafted player(s) back to the board?`)) {
      return;
    }
    setDraftedIds(new Set());
    setMyRosterIds(new Set());
    setDraftStarted(false);
    setMyPickPosition(null);
  };

  const available = useMemo(() => {
    if (!players) return [];
    return players.filter((p) => !draftedIds.has(p.player_id));
  }, [players, draftedIds]);

  const rosterCounts = useMemo(
    () => computeRosterCounts(players ?? [], myRosterIds),
    [players, myRosterIds]
  );

  const myRosterFull = myRosterIds.size >= FULL_ROSTER_SIZE;

  const currentRound = useMemo(() => computeCurrentRound(draftedIds.size), [draftedIds]);

  const scoreFn = STRATEGIES[activeStrategy].score;

  // Every available player, tagged with its strategy-adjusted score. Shared
  // by the recommendation callout and the "sort by strategy" toggle so the
  // score is only computed once per state change, not once per usage.
  const scoredAvailable = useMemo(() => {
    return available.map((p) => ({
      ...p,
      strategyScore: scoreFn(p, rosterCounts, currentRound),
    }));
  }, [available, rosterCounts, currentRound, scoreFn]);

  const recommendation = useMemo(() => {
    // Nothing left to recommend once your roster is full.
    if (myRosterIds.size >= FULL_ROSTER_SIZE) return null;

    // Force DEF/K onto the recommendation for your last 2 picks if you
    // don't have one yet. Without this override they'd never appear here
    // at all: crossPositionValue() deliberately pushes their score below
    // every skill position (see strategies.js), and the roster caps never
    // fully close off skill positions on their own — see PROJECTNOTES.md
    // "K/DEF late-round push" for the full story. Compares missing DEF/K
    // candidates by their own true VBD (not the suppressed cross-position
    // value) — the suppression's whole point was "don't take this too
    // early," which no longer applies once we're deliberately forcing it.
    if (myRosterIds.size >= FULL_ROSTER_SIZE - 2) {
      const missingPositions = ["DEF", "K"].filter((pos) => (rosterCounts[pos] ?? 0) === 0);
      if (missingPositions.length > 0) {
        const candidates = scoredAvailable.filter((p) => missingPositions.includes(p.position));
        if (candidates.length > 0) {
          const top = [...candidates].sort((a, b) => b.vbd - a.vbd)[0];
          return { top, alsoConsider: null, forced: true };
        }
      }
    }

    const eligible = scoredAvailable.filter((p) => !isPositionFull(p.position, rosterCounts));
    if (eligible.length === 0) return null;

    const byStrategy = [...eligible].sort(compareByScoreThenVBD);
    const byRawVBD = [...eligible].sort(
      (a, b) => crossPositionValue(b) - crossPositionValue(a)
    );
    const top = byStrategy[0];
    const alsoConsider = byRawVBD[0].player_id !== top.player_id ? byRawVBD[0] : null;

    return { top, alsoConsider, forced: false };
  }, [scoredAvailable, rosterCounts, myRosterIds]);

  const rosterView = useMemo(() => {
    if (!players) return null;
    return buildRosterSlots(players, myRosterIds);
  }, [players, myRosterIds]);

  const draftGrade = useMemo(() => {
    if (!players || myRosterIds.size < FULL_ROSTER_SIZE) return null;
    return computeDraftGrade(players, draftedIds, myRosterIds);
  }, [players, draftedIds, myRosterIds]);

  const rows = useMemo(() => {
    if (!players || activeTab === ROSTER_SLOT_TAB) return [];
    if (activeTab === "ALL") {
      const sorted = [...scoredAvailable];
      if (sortByStrategy) {
        // Same forced-DEF/K override as the Recommended Pick callout
        // (see the `recommendation` useMemo) — without this, "sort by
        // strategy + pick the top of the list" never surfaces DEF/K at
        // all until every skill position hits its MAX_ROSTER_COUNTS cap
        // (up to 16 skill picks), landing them 2 picks later than
        // intended. Missing DEF/K float to the very top when this fires,
        // ahead of even a boosted RB/WR score.
        const missingLatePositions =
          myRosterIds.size >= FULL_ROSTER_SIZE - 2
            ? new Set(["DEF", "K"].filter((pos) => (rosterCounts[pos] ?? 0) === 0))
            : new Set();

        // Capped positions (MAX_ROSTER_COUNTS) sink below everything still
        // eligible, same as the Recommended Pick callout already does —
        // otherwise a capped position with less-negative VBD than what's
        // left elsewhere (a common late-round pattern) floats back to the
        // top of "best remaining" even though the strategy has no more use
        // for it, undermining the whole point of sorting by strategy.
        sorted.sort((a, b) => {
          const aMissing = missingLatePositions.has(a.position);
          const bMissing = missingLatePositions.has(b.position);
          if (aMissing !== bMissing) return aMissing ? -1 : 1;

          const aFull = isPositionFull(a.position, rosterCounts);
          const bFull = isPositionFull(b.position, rosterCounts);
          if (aFull !== bFull) return aFull ? 1 : -1;
          return compareByScoreThenVBD(a, b);
        });
      } else {
        sorted.sort((a, b) => a.vbd_rank_overall - b.vbd_rank_overall);
      }
      return sorted;
    }
    return scoredAvailable
      .filter((p) => p.position === activeTab)
      .sort((a, b) => a.position_rank - b.position_rank);
  }, [players, activeTab, scoredAvailable, sortByStrategy, rosterCounts, myRosterIds]);

  // Group by tier only for a single-position view — tiers are computed
  // within each position, so a "Tier 1" QB and a "Tier 1" RB aren't
  // comparable. On the ALL tab we show a flat ranked list instead.
  const groupedByTier = useMemo(() => {
    if (activeTab === "ALL") return null;
    const groups = [];
    let currentTier = null;
    for (const player of rows) {
      if (player.tier !== currentTier) {
        currentTier = player.tier;
        groups.push({ tier: currentTier, players: [] });
      }
      groups[groups.length - 1].players.push(player);
    }
    return groups;
  }, [rows, activeTab]);

  return (
    <div className="app">
      <header className="scoreboard-header">
        <h1 className="scoreboard-header__title">Draft Board</h1>
        <p className="scoreboard-header__subtitle">
          14-team &middot; Full PPR &middot; Snake
        </p>
      </header>

      {!error && players && (
        <section className="recommendation-panel">
          <div className="recommendation-panel__controls">
            <div className="recommendation-panel__strategy">
              <label htmlFor="strategy-select">Strategy</label>
              <select
                id="strategy-select"
                value={activeStrategy}
                onChange={(e) => setActiveStrategy(e.target.value)}
              >
                {Object.entries(STRATEGIES).map(([key, { label }]) => (
                  <option key={key} value={key}>
                    {label}
                  </option>
                ))}
              </select>
            </div>
            <label className="autodraft-toggle">
              <input
                type="checkbox"
                checked={autodraftEnabled}
                onChange={(e) => setAutodraftEnabled(e.target.checked)}
              />
              Autodraft (testing)
            </label>
          </div>
          {recommendation ? (
            <div className="recommendation-panel__pick">
              <div className="recommendation-panel__pick-main">
                <span className="recommendation-panel__label">Recommended:</span>
                <PositionChip position={recommendation.top.position} />
                <span className="recommendation-panel__name">
                  {recommendation.top.player_display_name}
                </span>
              </div>
              {recommendation.forced ? (
                <span className="recommendation-panel__also">
                  you don't have a {recommendation.top.position} yet &mdash; grab one now
                </span>
              ) : (
                recommendation.alsoConsider && (
                  <span className="recommendation-panel__also">
                    also consider: <PositionChip position={recommendation.alsoConsider.position} />
                    {recommendation.alsoConsider.player_display_name}
                  </span>
                )
              )}
            </div>
          ) : (
            <div className="recommendation-panel__pick recommendation-panel__pick--empty">
              {myRosterFull
                ? "Your roster is full — reset to draft more."
                : "No recommendation available — roster targets filled or no players left."}
            </div>
          )}
        </section>
      )}

      <nav className="tabs">
        {TABS.map((tab) => (
          <button
            key={tab}
            className={`tab ${activeTab === tab ? "tab--active" : ""}`}
            onClick={() => setActiveTab(tab)}
          >
            {tab}
          </button>
        ))}
      </nav>

      <div className="draft-controls">
        <div className="draft-controls__left">
          <div className="start-draft">
            <label htmlFor="my-pick-position">My Pick #</label>
            <input
              id="my-pick-position"
              type="number"
              min={1}
              max={TEAM_COUNT}
              placeholder="1-14"
              value={myPickPosition ?? ""}
              disabled={draftStarted}
              onChange={handlePickPositionChange}
            />
            <button
              type="button"
              className="start-draft-button"
              onClick={handleStartDraft}
              disabled={draftStarted || !isValidPickPosition}
            >
              Start Draft
            </button>
          </div>
          <label
            className="sort-toggle"
            title="Only affects the ALL tab's ordering"
          >
            <input
              type="checkbox"
              checked={sortByStrategy}
              disabled={activeTab !== "ALL"}
              onChange={(e) => setSortByStrategy(e.target.checked)}
            />
            Sort by strategy
          </label>
        </div>
        <div className="draft-controls__status">
          <span className="drafted-count">{draftedIds.size} drafted</span>
          <span className="mine-count">{myRosterIds.size} on your roster</span>
        </div>
        <button
          className="reset-button"
          onClick={handleReset}
          disabled={draftedIds.size === 0}
        >
          Reset
        </button>
      </div>

      {!draftStarted && (
        <p className="start-draft-prompt">
          Enter your pick number and click Start Draft to begin.
        </p>
      )}

      <main className="board">
        {error && (
          <div className="empty-state">
            <p className="empty-state__title">Couldn't load the draft board.</p>
            <p className="empty-state__body">
              {error}. Run <code>scripts/csv_to_json.py</code> against your
              latest <code>cheat_sheet.csv</code> to generate{" "}
              <code>public/data/cheat_sheet.json</code>, then reload.
            </p>
          </div>
        )}

        {!error && !players && (
          <div className="empty-state">
            <p className="empty-state__title">Loading the board&hellip;</p>
          </div>
        )}

        {!error && players && activeTab === ROSTER_SLOT_TAB && (
          <section className={`draft-grade-panel${draftGrade ? ` draft-grade-panel--${draftGrade.grade[0]}` : ""}`}>
            {draftGrade ? (
              <>
                <span className="draft-grade-panel__grade">{draftGrade.grade}</span>
                <span className="draft-grade-panel__detail">
                  {draftGrade.percentDiff === null
                    ? `${draftGrade.yourTotal.toFixed(1)} proj pts — no other picks to compare against yet`
                    : `${draftGrade.yourTotal.toFixed(1)} proj pts vs. ${draftGrade.otherAvg.toFixed(1)} league avg (${draftGrade.percentDiff >= 0 ? "+" : ""}${draftGrade.percentDiff.toFixed(1)}%)`}
                </span>
              </>
            ) : (
              <span className="draft-grade-panel__locked">
                Draft grade unlocks at {FULL_ROSTER_SIZE} players on your roster ({myRosterIds.size}/
                {FULL_ROSTER_SIZE} so far)
              </span>
            )}
          </section>
        )}

        {!error && players && activeTab === ROSTER_SLOT_TAB && (
          <table className="board-table roster-table">
            <thead>
              <tr>
                <th className="col-slot">Slot</th>
                <th className="col-name">Player</th>
                <th className="col-num">Proj</th>
                <th className="col-num">VBD</th>
              </tr>
            </thead>
            <tbody>
              {rosterView.slots.map((slot) => (
                <RosterRow key={slot.label} slotLabel={slot.label} player={slot.player} />
              ))}
              {Array.from({ length: rosterView.benchSlotCount }, (_, i) => (
                <RosterRow
                  key={`bench-${i}`}
                  slotLabel={`BENCH ${i + 1}`}
                  player={rosterView.bench[i] ?? null}
                />
              ))}
              <RosterRow slotLabel="IR" player={null} />
            </tbody>
          </table>
        )}

        {!error && players && activeTab !== ROSTER_SLOT_TAB && (
          <table className="board-table">
            <thead>
              <tr>
                <th className="col-mine" aria-label="Drafted by you" />
                <th className="col-rank">Rk</th>
                <th className="col-name">Player</th>
                <th className="col-num">Proj</th>
                <th className="col-num">VBD</th>
                <th className="col-value">vs ADP</th>
              </tr>
            </thead>
            <tbody>
              {activeTab === "ALL"
                ? rows.map((player) => (
                    <PlayerRow
                      key={player.player_id}
                      player={player}
                      rankField="vbd_rank_overall"
                      onDraft={handleDraft}
                      onDraftMine={handleDraftMine}
                      myRosterFull={myRosterFull}
                      draftStarted={draftStarted}
                    />
                  ))
                : groupedByTier.map((group) => (
                    <FragmentGroup
                      key={group.tier}
                      group={group}
                      onDraft={handleDraft}
                      onDraftMine={handleDraftMine}
                      myRosterFull={myRosterFull}
                      draftStarted={draftStarted}
                    />
                  ))}
            </tbody>
          </table>
        )}
      </main>
    </div>
  );
}

function FragmentGroup({ group, onDraft, onDraftMine, myRosterFull, draftStarted }) {
  return (
    <>
      <TierDivider tier={group.tier} />
      {group.players.map((player) => (
        <PlayerRow
          key={player.player_id}
          player={player}
          rankField="position_rank"
          onDraft={onDraft}
          onDraftMine={onDraftMine}
          myRosterFull={myRosterFull}
          draftStarted={draftStarted}
        />
      ))}
    </>
  );
}
