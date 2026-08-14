"""
config.py

Tunable settings for the projection model, kept separate from
projection_model.py so they're easy to find and adjust without digging
through the data pipeline logic.
"""

# ---------------------------------------------------------------------------
# Projection weighting
# ---------------------------------------------------------------------------

# How many of a player's most recent seasons to weight into the projection.
LOOKBACK_SEASONS = 5

# Exponential decay applied per season of recency. weight = DECAY ** n,
# where n=0 is the most recent season. Lower = more weight on recent data.
DECAY = 0.65

# Max games in a modern NFL regular season (used to cap projected games).
MAX_GAMES = 17

# ---------------------------------------------------------------------------
# Age adjustment
# ---------------------------------------------------------------------------

# Position-specific age curves: (peak_age, decline_per_year_past_peak).
# Below peak_age, no penalty is applied (rookies/young players aren't
# penalized — their limited sample already reflects less-established roles).
# This is intentionally simple; a real aging curve is nonlinear, but this
# gives a directionally correct adjustment without overfitting to a
# historical-only sample. These are starting assumptions, not fitted to
# data — treat as tunable knobs.
AGE_CURVES = {
    "RB": (27, 0.06),   # RBs decline earliest and steepest
    "WR": (29, 0.04),
    "TE": (29, 0.03),
    "QB": (32, 0.02),   # QBs decline latest and slowest
}


def age_adjustment_factor(position: str, age: float | None) -> float:
    if age is None or position not in AGE_CURVES:
        return 1.0
    peak_age, decline_per_year = AGE_CURVES[position]
    if age <= peak_age:
        return 1.0
    years_past_peak = age - peak_age
    factor = 1.0 - (decline_per_year * years_past_peak)
    return max(factor, 0.4)  # floor so projections don't go negative/absurd