"""
vbd_and_tiers.py

Step 3 of the fantasy draft tool: turn raw projections into draft-ready
values by computing Value-Based Drafting (VBD) scores and grouping players
into tiers. This is what the pre-draft cheat sheet is actually built from —
raw projected points alone don't account for positional scarcity.

NOTE: Not run/tested against live data (no network access in the
environment this was written in). Run locally and sanity-check the output.

Requires config.py (same directory) for league/roster settings.

Usage:
    python vbd_and_tiers.py
    python vbd_and_tiers.py --force
"""

from pathlib import Path

import polars as pl

from lib.config import (
    LEAGUE_SIZE,
    ROSTER_SLOTS,
    FLEX_SLOTS,
    FLEX_ELIGIBLE_POSITIONS,
    TIER_GAP_PCT_THRESHOLD,
    TIER_MIN_GAP_POINTS,
)

# Positions covered by the projection model so far (K/DEF need separate
# data — see compute_fantasy_points.py notes).
PROJECTED_POSITIONS = ["QB", "RB", "WR", "TE"]


def load_projections() -> pl.DataFrame:
    path = Path("lib/projections.csv")
    if not path.exists():
        raise FileNotFoundError(f"{path} not found — run projection_model.py first.")
    return pl.read_csv(path)


def compute_starters_per_position(projections: pl.DataFrame) -> dict[str, int]:
    """
    Determine how many players at each position are actually "starter"
    value across the league, including FLEX slots. FLEX slots are assigned
    dynamically: take the best remaining FLEX-eligible players (by
    projected points, after guaranteed starters are removed) league-wide,
    up to LEAGUE_SIZE * FLEX_SLOTS of them, and count however many of each
    position end up in that pool. This reflects realistic FLEX usage
    (e.g. FLEX slots tend to go to RBs/WRs more than TEs) rather than a
    static split.
    """
    guaranteed_starters = {
        pos: ROSTER_SLOTS.get(pos, 0) * LEAGUE_SIZE for pos in PROJECTED_POSITIONS
    }

    # Pool of FLEX-eligible players not already accounted for by guaranteed
    # starters, ranked by projected points, to fill FLEX slots from.
    flex_candidates = []
    for pos in FLEX_ELIGIBLE_POSITIONS:
        if pos not in PROJECTED_POSITIONS:
            continue
        pos_players = (
            projections.filter(pl.col("position") == pos)
            .sort("projected_fantasy_points", descending=True)
        )
        # Skip the guaranteed starters for this position — only players
        # beyond that are candidates for FLEX.
        beyond_starters = pos_players.slice(guaranteed_starters[pos])
        for row in beyond_starters.iter_rows(named=True):
            flex_candidates.append((pos, row["projected_fantasy_points"]))

    flex_candidates.sort(key=lambda x: x[1], reverse=True)
    total_flex_slots = LEAGUE_SIZE * FLEX_SLOTS
    flex_fill = flex_candidates[:total_flex_slots]

    flex_counts = {pos: 0 for pos in FLEX_ELIGIBLE_POSITIONS}
    for pos, _ in flex_fill:
        flex_counts[pos] += 1

    starters = dict(guaranteed_starters)
    for pos, count in flex_counts.items():
        starters[pos] = starters.get(pos, 0) + count

    return starters


def compute_vbd(projections: pl.DataFrame, starters: dict[str, int]) -> pl.DataFrame:
    """
    For each position, the replacement-level baseline is the projected
    points of the player ranked immediately after the last starter slot
    at that position (guaranteed + FLEX share). VBD = projected points
    minus that baseline. This is a VORP-style baseline (replacement
    player), not VOLS (last starter) — slightly more conservative, and
    accounts for FLEX competition since starters[] already includes it.
    """
    result_frames = []

    for pos in PROJECTED_POSITIONS:
        pos_players = (
            projections.filter(pl.col("position") == pos)
            .sort("projected_fantasy_points", descending=True)
        )

        num_starters = starters.get(pos, 0)

        if pos_players.height > num_starters:
            baseline = pos_players["projected_fantasy_points"][num_starters]
        else:
            # Fewer players projected than starter slots (shouldn't
            # normally happen) — fall back to the last available player.
            baseline = pos_players["projected_fantasy_points"][-1]

        pos_players = pos_players.with_columns(
            (pl.col("projected_fantasy_points") - baseline).alias("vbd")
        )
        result_frames.append(pos_players)

    combined = pl.concat(result_frames)

    # Overall (cross-position) VBD rank — this is what actually determines
    # draft order in practice, unlike the position-scoped position_rank
    # from the projection model.
    combined = combined.with_columns(
        pl.col("vbd")
        .rank(method="ordinal", descending=True)
        .cast(pl.Int64)
        .alias("vbd_rank_overall")
    )

    # Cross-reference against the overall ADP proxy (from projection_model.py):
    # positive = VBD ranks this player earlier than their recent real-world
    # finish would suggest (VBD thinks the "market" is undervaluing them);
    # negative = VBD is more cautious than their recent finish (e.g. QBs,
    # which naive VBD tends to over-value in single-QB formats — see caveat
    # below).
    if "adp_proxy_rank_overall" in combined.columns:
        combined = combined.with_columns(
            (pl.col("adp_proxy_rank_overall") - pl.col("vbd_rank_overall")).alias(
                "vbd_vs_adp_proxy_overall"
            )
        )

    return combined


def assign_tiers(projections_with_vbd: pl.DataFrame) -> pl.DataFrame:
    """
    Within each position's FULL ranked list (no candidate-pool cutoff —
    see TIER_MIN_GAP_POINTS in config.py for why an earlier top-40-only
    cutoff dumped everyone else into one mega-tier), find gaps between
    consecutive players' projected points. A new tier starts when a gap
    exceeds BOTH TIER_GAP_PCT_THRESHOLD (of the higher-ranked player's
    points) AND TIER_MIN_GAP_POINTS (a flat point minimum) — percentage
    alone is unstable near the bottom of the list where points approach
    0, and a flat minimum alone is meaningless at the top where a few
    points is noise between elite players. (Points, not VBD, for the
    percentage base — VBD crosses 0 partway down a position's list, which
    makes percentage-of-VBD unstable; the gap itself is identical either
    way since VBD is just points minus a constant per-position baseline.)
    """
    result_frames = []

    for pos in PROJECTED_POSITIONS:
        pos_players = (
            projections_with_vbd.filter(pl.col("position") == pos)
            .sort("vbd", descending=True)
        )

        points_values = pos_players["projected_fantasy_points"].to_list()

        tiers = [1]
        current_tier = 1
        for i in range(len(points_values) - 1):
            gap = points_values[i] - points_values[i + 1]
            gap_pct = gap / points_values[i] if points_values[i] > 0 else 1.0
            if gap_pct > TIER_GAP_PCT_THRESHOLD and gap > TIER_MIN_GAP_POINTS:
                current_tier += 1
            tiers.append(current_tier)

        pos_players = pos_players.with_columns(pl.Series("tier", tiers, dtype=pl.Int64))
        result_frames.append(pos_players)

    return pl.concat(result_frames)


def main(force_recompute: bool = False):
    out_path = Path("lib/cheat_sheet.csv")

    if out_path.exists() and not force_recompute:
        print(f"{out_path} already exists — skipping recompute. (pass --force to redo)")
        result = pl.read_csv(out_path)
    else:
        print("Loading projections...")
        projections = load_projections()

        print(f"Computing starter counts per position (league size={LEAGUE_SIZE})...")
        starters = compute_starters_per_position(projections)
        print(f"  Starters (incl. FLEX share): {starters}")

        print("Computing VBD (replacement-level baseline per position)...")
        with_vbd = compute_vbd(projections, starters)

        print("Assigning tiers within each position...")
        with_tiers = assign_tiers(with_vbd)

        result = with_tiers.sort("vbd", descending=True)
        result.write_csv(out_path)
        print(f"Done. Wrote {result.height} players to {out_path}")

    print("\nTop 20 overall by VBD (with ADP proxy cross-reference):")
    with pl.Config(tbl_rows=20):
        print(
            result.sort("vbd", descending=True)
            .head(20)
            .select(
                [
                    "player_display_name",
                    "position",
                    "projected_fantasy_points",
                    "vbd",
                    "tier",
                    "vbd_rank_overall",
                    "adp_proxy_rank_overall",
                    "vbd_vs_adp_proxy_overall",
                ]
            )
        )


if __name__ == "__main__":
    import sys

    main(force_recompute="--force" in sys.argv)