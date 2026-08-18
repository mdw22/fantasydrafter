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
// it — keeps the recommendation from suggesting a 4th bench QB just because
// it's still technically the highest-raw-VBD player left. Not roster-slot-
// exact (FLEX/bench blur the line) — a deliberately generous "nobody would
// actually take this" backstop, not a hard roster-legality check.
export const MAX_ROSTER_COUNTS = { QB: 3, RB: 6, WR: 6, TE: 3 };

export function isPositionFull(position, rosterCounts) {
  return (rosterCounts[position] ?? 0) >= (MAX_ROSTER_COUNTS[position] ?? Infinity);
}

// --- Robust RB ---

const TARGET_RB_COUNT = 3; // tunable — how many RBs "robust" wants
const RB_BOOST_MULTIPLIER = 1.35; // tunable — how hard to prioritize RB
const BOOST_TAPER_START_ROUND = 6; // tunable — start softening the boost here

function robustRBScore(player, rosterCounts, currentRound) {
  if (player.position !== "RB") return player.vbd;
  if ((rosterCounts.RB ?? 0) >= TARGET_RB_COUNT) return player.vbd;

  let multiplier = RB_BOOST_MULTIPLIER;
  if (currentRound > BOOST_TAPER_START_ROUND) {
    // Taper toward 1.0x the later it gets, so this doesn't force an absurd
    // reach on a bench RB in round 12 just because you're still one short
    // of the target.
    const roundsPastTaper = currentRound - BOOST_TAPER_START_ROUND;
    multiplier = Math.max(1.0, multiplier - 0.1 * roundsPastTaper);
  }
  return player.vbd * multiplier;
}

export const STRATEGIES = {
  robustRB: { label: "Robust RB", score: robustRBScore },
};

export const DEFAULT_STRATEGY = "robustRB";

export function compareByScoreThenVBD(a, b) {
  if (b.strategyScore !== a.strategyScore) return b.strategyScore - a.strategyScore;
  return b.vbd - a.vbd;
}
