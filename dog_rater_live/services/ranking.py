"""Rank a race field with the existing heuristic scorer."""

from __future__ import annotations

from typing import Any, Optional

from models import Runner
from scoring import RankedRunner, normalize_weights, rank_runners, suggest_auto_weights
from services.confidence import confidence_from_scores


def active_runners(runners: list[Runner] | None) -> list[Runner]:
    out: list[Runner] = []
    for r in runners or []:
        if bool(getattr(r, "scratched", False)):
            continue
        name = str(getattr(r, "name", "") or "").strip()
        if not name:
            continue
        out.append(r)
    return out


def rank_field(
    runners: list[Runner] | None,
    *,
    track_condition: Optional[str] = None,
    weather: Any = None,
    box_weight: Optional[float] = None,
    form_weight: Optional[float] = None,
    early_weight: Optional[float] = None,
    explain_mode: str = "short",
) -> tuple[list[RankedRunner], tuple[float, float, float], list[str]]:
    field = active_runners(runners)
    if not field:
        return [], (1 / 3, 1 / 3, 1 / 3), ["No active runners."]
    if box_weight is None or form_weight is None or early_weight is None:
        bw, fw, ew, rationale = suggest_auto_weights(
            field, weather=weather, track_condition=track_condition
        )
    else:
        bw, fw, ew = normalize_weights(box_weight, form_weight, early_weight)
        rationale = ["Manual weights from Model controls."]
    ranked = rank_runners(
        field,
        box_weight=bw,
        form_weight=fw,
        early_weight=ew,
        weather=weather,
        track_condition=track_condition,
        explain_mode=explain_mode,
    )
    return ranked, (bw, fw, ew), rationale


def selections_from_ranked(ranked: list[RankedRunner]) -> dict[str, Any]:
    primary = ranked[0] if ranked else None
    backup = ranked[1] if len(ranked) > 1 else None
    gap, label = confidence_from_scores(
        getattr(primary, "score", None) if primary else None,
        getattr(backup, "score", None) if backup else None,
    )
    return {
        "primary": getattr(primary, "name", "") if primary else "",
        "backup": getattr(backup, "name", "") if backup else "",
        "primary_score": float(getattr(primary, "score", 0.0) or 0.0) if primary else None,
        "backup_score": float(getattr(backup, "score", 0.0) or 0.0) if backup else None,
        "primary_draw": getattr(primary, "draw", None) if primary else None,
        "backup_draw": getattr(backup, "draw", None) if backup else None,
        "primary_why": list(getattr(primary, "why_bullets", []) or [])[:8] if primary else [],
        "backup_why": list(getattr(backup, "why_bullets", []) or [])[:8] if backup else [],
        "key_factors": str(getattr(primary, "key_factors", "") or "") if primary else "",
        "score_gap": gap,
        "confidence_label": label if primary else "",
        "ranked": ranked,
    }
