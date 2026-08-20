// Draft strategy scoring. Each strategy is a small, isolated function of the
// same shape — (player, rosterCounts, currentRound) -> adjusted score — so
// the UI's strategy selector can switch between them and new strategies
// (Zero RB, Hero RB, Balanced) can be registered later without restructuring.

export const TEAM_COUNT = 14;

export function computeCurrentRound(totalDraftedCount) {
  return Math.floor(totalDraftedCount / TEAM_COUNT) + 1;
}

// Snake draft: odd rounds run 1..N, even rounds reverse to N..1. Your
// overall pick number in a given round, for a fixed draft position
// myPickPosition (1..teamCount).
export function computeSnakePickNumber(round, myPickPosition, teamCount = TEAM_COUNT) {
  return round % 2 === 1
    ? (round - 1) * teamCount + myPickPosition
    : (round - 1) * teamCount + (teamCount - myPickPosition + 1);
}

// How many opponent picks happen between your pick in `round` and your
// pick in `round + 1`. Alternates by parity: going odd->even the gap is
// 2*(teamCount - myPickPosition); going even->odd it's 2*(myPickPosition
// - 1). Any two consecutive gaps always sum to 2*(teamCount - 1) — the
// total "other" picks across any 2 consecutive rounds — a useful
// invariant to test against regardless of position.
export function computeOpponentGap(round, myPickPosition, teamCount = TEAM_COUNT) {
  const current = computeSnakePickNumber(round, myPickPosition, teamCount);
  const next = computeSnakePickNumber(round + 1, myPickPosition, teamCount);
  return next - current - 1;
}

export function computeRosterCounts(players, myRosterIds) {
  const counts = { QB: 0, RB: 0, WR: 0, TE: 0, K: 0, DEF: 0 };
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
// QB/TE capped at 2, RB/WR capped at 6 — per user direction. K/DEF capped
// at 1 each — there's exactly one K slot and one DEF slot on the roster,
// so a 2nd of either is never useful in a redraft league.
export const MAX_ROSTER_COUNTS = { QB: 2, RB: 6, WR: 6, TE: 2, K: 1, DEF: 1 };

export function isPositionFull(position, rosterCounts) {
  return (rosterCounts[position] ?? 0) >= (MAX_ROSTER_COUNTS[position] ?? Infinity);
}

// K/DEF's raw VBD can look numerically competitive with mid-tier skill
// positions, but real draft convention is to never touch K/DEF until the
// very last picks — their week-to-week value is far more replaceable/
// volatile than the point spread suggests. This offset is large enough
// that even the single best K/DEF's cross-position value still lands
// below the worst rostered skill-position player's true VBD (skill VBD
// floor was ~-230 in the data checked when this was picked, K/DEF tops
// out around 40-60). Same offset used server-side for vbd_rank_overall
// in vbd_and_tiers.py (LATE_ROUND_VBD_OFFSET in config.py) — kept
// manually in sync. Applied only to cross-position comparisons (this
// function, and the "also consider" raw-BPA fallback in App.jsx) — never
// to player.vbd itself, so K/DEF's own tab still shows true VBD/tiers.
const LATE_ROUND_POSITIONS = new Set(["K", "DEF"]);
const LATE_ROUND_VBD_OFFSET = 300;

export function crossPositionValue(player) {
  return LATE_ROUND_POSITIONS.has(player.position)
    ? player.vbd - LATE_ROUND_VBD_OFFSET
    : player.vbd;
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
  // QB/TE/K/DEF: no boost, just the MAX_ROSTER_COUNTS cap above (and,
  // for K/DEF, the late-round push via crossPositionValue).
  if (!floor) return crossPositionValue(player);
  if ((rosterCounts[player.position] ?? 0) >= floor.targetCount) return player.vbd;

  return player.vbd * taperedMultiplier(floor.multiplier, currentRound);
}

// --- Hero RB ---
// Opposite shape from Robust RB at RB specifically: grab exactly ONE elite
// RB early (boost), then actively avoid a 2nd/3rd RB for a long stretch
// (suppress) so that draft capital goes to WR/TE instead (boost), before
// RB returns to neutral/raw VBD late for cheap depth and handcuffs.

const TARGET_HERO_RB_COUNT = 1;
const HERO_RB_BOOST_MULTIPLIER = 1.45; // higher than Robust RB's 1.35 — missing the one hero RB is this strategy's single biggest risk
const HERO_RB_MIN_TIER = 2; // quality floor — only an elite/near-elite RB qualifies as "the hero pick"; a mediocre RB1 shouldn't get boosted into looking like a priority

const RB_SUPPRESSION_MULTIPLIER = 0.7; // actively discourage RB #2+ during the pivot window — the strategy working as intended, not a bug, so no quality floor here
const RB_SUPPRESSION_END_ROUND = 10; // pivot window ends here; RB scoring returns to neutral (raw VBD) after this round

const WR_PIVOT_BOOST_MULTIPLIER = 1.3; // slightly stronger than Robust RB's WR floor — Hero RB leans harder into WR specifically
const TE_PIVOT_BOOST_MULTIPLIER = 1.1; // smaller than WR's — real Hero RB builds lean WR much harder than TE
const MAX_TIER_FOR_BOOST = 4; // quality floor for the WR/TE pivot boost — reuses Robust RB's established value

function heroRBScore(player, rosterCounts, currentRound) {
  const rbCount = rosterCounts.RB ?? 0;
  const inPivotWindow = rbCount >= TARGET_HERO_RB_COUNT && currentRound <= RB_SUPPRESSION_END_ROUND;

  if (player.position === "RB") {
    if (rbCount < TARGET_HERO_RB_COUNT) {
      // The hero pick itself — only an elite/near-elite RB earns the boost.
      if (player.tier <= HERO_RB_MIN_TIER) return player.vbd * HERO_RB_BOOST_MULTIPLIER;
      return player.vbd;
    }
    if (inPivotWindow) return player.vbd * RB_SUPPRESSION_MULTIPLIER;
    return player.vbd; // past the pivot window — cheap late RB depth/handcuffs are fine again
  }

  if (inPivotWindow && player.tier <= MAX_TIER_FOR_BOOST) {
    if (player.position === "WR") return player.vbd * WR_PIVOT_BOOST_MULTIPLIER;
    if (player.position === "TE") return player.vbd * TE_PIVOT_BOOST_MULTIPLIER;
  }

  return crossPositionValue(player);
}

export const STRATEGIES = {
  robustRB: { label: "Robust RB", score: robustRBScore },
  heroRB: { label: "Hero RB", score: heroRBScore },
};

export const DEFAULT_STRATEGY = "robustRB";

export function compareByScoreThenVBD(a, b) {
  if (b.strategyScore !== a.strategyScore) return b.strategyScore - a.strategyScore;
  return crossPositionValue(b) - crossPositionValue(a);
}
