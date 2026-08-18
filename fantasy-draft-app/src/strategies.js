// Draft strategy scoring. Each strategy is a small, isolated function of the
// same shape — (player, rosterCounts, currentRound) -> adjusted score — so
// the UI's strategy selector can switch between them and new strategies
// (Zero RB, Hero RB, Balanced) can be registered later without restructuring.

export const TEAM_COUNT = 14;

export function computeCurrentRound(totalDraftedCount) {
  return Math.floor(totalDraftedCount / TEAM_COUNT) + 1;
}

export function computeRosterCounts(players, myRosterIds) {
  const counts = { QB: 0, RB: 0, WR: 0, TE: 0 };
  for (const player of players) {
    if (myRosterIds.has(player.player_id)) {
      counts[player.position] = (counts[player.position] ?? 0) + 1;
    }
  }
  return counts;
}

// Soft cap on roster depth per position before we stop recommending more of
// it — keeps the recommendation from suggesting a 3rd bench QB just because
// it's still technically the highest-raw-VBD player left. Not roster-slot-
// exact (FLEX/bench blur the line) — a deliberately generous "nobody would
// actually take this" backstop, not a hard roster-legality check.
// QB/TE capped at 2, RB/WR capped at 6 — per user direction.
export const MAX_ROSTER_COUNTS = { QB: 2, RB: 6, WR: 6, TE: 2 };

export function isPositionFull(position, rosterCounts) {
  return (rosterCounts[position] ?? 0) >= (MAX_ROSTER_COUNTS[position] ?? Infinity);
}

// --- Robust RB ---
// Despite the name, this also enforces a WR floor and (via
// MAX_ROSTER_COUNTS above) QB/TE caps — the full set of roster-
// construction guardrails discussed with the user, not just an RB boost.

const BOOST_TAPER_START_ROUND = 6; // tunable — start softening either boost here, shared by both

// Per-position "keep boosting until you have at least this many" floors.
// WR mirrors RB's target count and multiplier by default since no separate
// magnitude was specified — independently tunable here if that changes.
const POSITION_FLOORS = {
  RB: { targetCount: 3, multiplier: 1.35 }, // tunable — minimum RBs "robust" wants, and how hard to prioritize them
  WR: { targetCount: 3, multiplier: 1.35 }, // tunable — minimum WRs "robust" wants, and how hard to prioritize them
};

function taperedMultiplier(baseMultiplier, currentRound) {
  if (currentRound <= BOOST_TAPER_START_ROUND) return baseMultiplier;
  // Taper toward 1.0x the later it gets, so this doesn't force an absurd
  // reach on a bench player in round 12 just because you're still short of
  // the target.
  const roundsPastTaper = currentRound - BOOST_TAPER_START_ROUND;
  return Math.max(1.0, baseMultiplier - 0.1 * roundsPastTaper);
}

function robustRBScore(player, rosterCounts, currentRound) {
  const floor = POSITION_FLOORS[player.position];
  if (!floor) return player.vbd; // QB/TE: no boost, just the MAX_ROSTER_COUNTS cap above
  if ((rosterCounts[player.position] ?? 0) >= floor.targetCount) return player.vbd;

  return player.vbd * taperedMultiplier(floor.multiplier, currentRound);
}

export const STRATEGIES = {
  robustRB: { label: "Robust RB", score: robustRBScore },
};

export const DEFAULT_STRATEGY = "robustRB";

export function compareByScoreThenVBD(a, b) {
  if (b.strategyScore !== a.strategyScore) return b.strategyScore - a.strategyScore;
  return b.vbd - a.vbd;
}
