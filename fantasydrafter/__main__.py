import bin.compute_fantasy_points as compute_fantasy_points
import bin.projection_model as projection_model
import bin.vbd_and_tiers as vbd_and_tiers

def callInjuryCheck() :
    import polars as pl

    df = pl.read_csv("lib/projections.csv")
    match = df.filter(pl.col("player_display_name") == "Tyreek Hill")
    if match.height == 0:
        print("Tyreek Hill not in projections.csv (excluded — roster status was in EXCLUDED_ROSTER_STATUSES).")
    else:
        print(match.select(["player_display_name", "roster_status", "projected_fantasy_points"]))

def main(force_recompute: bool):
    # compute_fantasy_points gives a summary of top ten finishes in the last 20+ years
    # Not neccessary to run except for the generated fantasy_points_by_season csv file
    compute_fantasy_points.main(force_recompute)
    # Models the projection based on rules found in config.py
    projection_model.main(force_recompute)

    vbd_and_tiers.main(force_recompute)

    callInjuryCheck()


if __name__ == "__main__":
    import sys

    main(force_recompute="--force" in sys.argv)