"""Confidence labels from model score gap.

These are heuristic labels for the dashboard, not statistically validated
probabilities. Thresholds match the existing roster "clear gap" of 0.05.
"""

from __future__ import annotations

from typing import Optional

# Characterisation: roster "just place" treats gap >= 0.05 as a clear favourite.
STRONG_GAP = 0.05
MEDIUM_GAP = 0.02

LABEL_STRONG = "Strong"
LABEL_MEDIUM = "Medium"
LABEL_CLOSE = "Close race"


def score_gap(primary_score: Optional[float], backup_score: Optional[float]) -> float:
    try:
        a = float(primary_score or 0.0)
        b = float(backup_score or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return a - b


def confidence_label(gap: Optional[float]) -> str:
    """Map Rank1−Rank2 score gap to a human label."""
    try:
        g = float(gap or 0.0)
    except (TypeError, ValueError):
        g = 0.0
    if g >= STRONG_GAP:
        return LABEL_STRONG
    if g >= MEDIUM_GAP:
        return LABEL_MEDIUM
    return LABEL_CLOSE


def confidence_from_scores(primary_score: Optional[float], backup_score: Optional[float]) -> tuple[float, str]:
    gap = score_gap(primary_score, backup_score)
    return gap, confidence_label(gap)
