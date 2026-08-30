"""Immutable pick snapshots.

Once a pick is locked (user confirm, or automatically at/near jump), later live
rankings must not overwrite the stored primary/backup/scores/odds/weights.
A primary scratching updates flags only — the original pick name is preserved.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from journal import make_pick_entry
from race_db import (
    _DEFAULT_DB,
    get_pick,
    lock_pick,
    mark_primary_scratched,
    save_pick,
)
from services.confidence import confidence_from_scores
from services.runner_numbers import (
    coerce_program_number,
    number_from_field_snapshot,
    program_number_for_runner,
)

LOCK_BEFORE_JUMP = timedelta(minutes=2)


def _as_date(d: date | str) -> date:
    if isinstance(d, date):
        return d
    return date.fromisoformat(str(d)[:10])


def build_snapshot_payload(
    *,
    meeting_date: date,
    code: str,
    venue: str,
    meeting_url: str,
    race_no: int,
    race_name: str,
    race_url: str,
    primary: str,
    backup: str,
    primary_score: Optional[float],
    backup_score: Optional[float],
    primary_draw: Optional[int] = None,
    key_factors: str = "",
    why_bullets: Optional[list[str]] = None,
    weights: Optional[dict[str, Any]] = None,
    field: Optional[list[dict[str, Any]]] = None,
    scratching_state: Optional[list[dict[str, Any]]] = None,
    primary_odds: Optional[float] = None,
    backup_odds: Optional[float] = None,
    primary_number: Optional[int] = None,
    backup_number: Optional[int] = None,
    scheduled_jump: str = "",
    track_condition: str = "",
    status: str = "",
    field_size: Optional[int] = None,
) -> dict[str, Any]:
    gap, label = confidence_from_scores(primary_score, backup_score)
    entry = make_pick_entry(
        meeting_date=meeting_date,
        code=code,
        venue=venue,
        meeting_url=meeting_url,
        race_no=int(race_no),
        race_name=race_name or f"R{race_no}",
        race_url=race_url or "",
        pick_name=primary,
        pick_draw=primary_draw,
        pick_score=float(primary_score or 0.0),
        key_factors=key_factors or "",
        why_bullets=list(why_bullets or [])[:8],
        history_bullets=[],
        weights=weights or {},
        conditions={
            "backup": backup,
            "backup_score": backup_score,
            "field_size": field_size,
            "status": status,
            "track_condition": track_condition,
            "scheduled_jump": scheduled_jump,
        },
    )
    payload = dict(entry.__dict__)
    payload["backup"] = backup or ""
    if primary_number is None:
        primary_number = number_from_field_snapshot(field, primary)
    if backup_number is None:
        backup_number = number_from_field_snapshot(field, backup)
    primary_number = coerce_program_number(primary_number)
    backup_number = coerce_program_number(backup_number)
    payload["primary_number"] = primary_number
    payload["backup_number"] = backup_number
    payload["snapshot"] = {
        "confidence_label": label,
        "score_gap": gap,
        "primary_odds": primary_odds,
        "backup_odds": backup_odds,
        "primary_number": primary_number,
        "backup_number": backup_number,
        "scheduled_jump": scheduled_jump,
        "field": list(field or []),
        "scratching_state": list(scratching_state or []),
        "weights": weights or {},
        "captured_at": entry.picked_at_iso,
    }
    payload["confidence_label"] = label
    payload["score_gap"] = gap
    payload["scheduled_jump"] = scheduled_jump
    return payload


def save_selection_snapshot(
    *,
    meeting_date: date | str,
    meeting_url: str,
    code: str,
    race_no: int,
    venue: str,
    race_label: str,
    primary: str,
    backup: str = "",
    pick_data: Optional[dict[str, Any]] = None,
    best_score: Optional[float] = None,
    backup_score: Optional[float] = None,
    field_size: Optional[int] = None,
    status: str = "",
    confidence_label: str = "",
    score_gap: Optional[float] = None,
    primary_odds: Optional[float] = None,
    backup_odds: Optional[float] = None,
    scheduled_jump: str = "",
    lock: bool = False,
    db_path: Path = _DEFAULT_DB,
) -> bool:
    """Write a snapshot. No-op (returns False) if an existing row is locked."""
    d = _as_date(meeting_date)
    existing = get_pick(d, meeting_url, int(race_no), db_path=db_path)
    if existing and existing.get("locked") and not lock:
        return False
    if existing and existing.get("locked"):
        # Confirm/lock of an already-saved snapshot: lock only, do not rewrite.
        return lock_pick(d, meeting_url, int(race_no), db_path=db_path)

    snap = (pick_data or {}).get("snapshot") or {}
    if not confidence_label:
        if score_gap is None:
            score_gap, confidence_label = confidence_from_scores(best_score, backup_score)
        else:
            from services.confidence import confidence_label as _lab

            confidence_label = _lab(score_gap)
    return save_pick(
        d,
        meeting_url,
        code,
        int(race_no),
        venue,
        race_label or f"R{race_no}",
        primary,
        backup=backup,
        pick_data=pick_data,
        best_score=best_score,
        backup_score=backup_score,
        field_size=field_size,
        status=status,
        confidence_label=confidence_label or str(snap.get("confidence_label") or ""),
        score_gap=score_gap if score_gap is not None else snap.get("score_gap"),
        primary_odds=primary_odds if primary_odds is not None else snap.get("primary_odds"),
        backup_odds=backup_odds if backup_odds is not None else snap.get("backup_odds"),
        scheduled_jump=scheduled_jump or str(snap.get("scheduled_jump") or ""),
        primary_number=coerce_program_number((pick_data or {}).get("primary_number"))
        or coerce_program_number(snap.get("primary_number")),
        backup_number=coerce_program_number((pick_data or {}).get("backup_number"))
        or coerce_program_number(snap.get("backup_number")),
        locked=lock,
        locked_at=(datetime.now().timestamp() if lock else None),
        db_path=db_path,
    )


def confirm_pick(
    meeting_date: date | str,
    meeting_url: str,
    race_no: int,
    db_path: Path = _DEFAULT_DB,
) -> bool:
    return lock_pick(_as_date(meeting_date), meeting_url, int(race_no), db_path=db_path)


def maybe_autolock(
    pick: dict[str, Any],
    *,
    now: datetime,
    jump_at: Optional[datetime],
    db_path: Path = _DEFAULT_DB,
) -> dict[str, Any]:
    """Lock a stored pick when the race is within the pre-jump window or has jumped."""
    if pick.get("locked"):
        return pick
    if jump_at is None:
        return pick
    if now + LOCK_BEFORE_JUMP < jump_at:
        return pick
    d = _as_date(pick.get("meeting_date") or pick.get("date"))
    meeting_url = str(pick.get("meeting_url") or "")
    race_no = int(pick.get("race_no") or 0)
    if not meeting_url or not race_no:
        return pick
    lock_pick(d, meeting_url, race_no, db_path=db_path)
    updated = get_pick(d, meeting_url, race_no, db_path=db_path)
    return updated or {**pick, "locked": True}


def record_primary_scratching(
    meeting_date: date | str,
    meeting_url: str,
    race_no: int,
    db_path: Path = _DEFAULT_DB,
    **kwargs,
) -> dict[str, Any] | None:
    d = _as_date(meeting_date)
    mark_primary_scratched(d, meeting_url, int(race_no), db_path=db_path, **kwargs)
    return get_pick(d, meeting_url, int(race_no), db_path=db_path)


def apply_view_scratching(
    view,
    *,
    chosen_date: date | str,
    db_path: Path = _DEFAULT_DB,
    now: Optional[datetime] = None,
) -> dict[str, Any] | None:
    """Persist late-scratching flags and unlocked rerank. Idempotent. Does not rewrite locked originals."""
    from services.scratching import log_late_scratch_transition

    if not getattr(view, "primary_scratched", False) and not getattr(view, "backup_scratched", False):
        return None
    d = _as_date(chosen_date)
    existing = get_pick(d, view.meeting_url, int(view.race_no), db_path=db_path)
    sources = []
    for _name, srcs in (getattr(view, "scratching_sources", None) or {}).items():
        for s in srcs or []:
            if s not in sources:
                sources.append(s)
    source = ",".join(sources) or "sportsbet"
    detected = getattr(view, "scratch_confirmed_at", "") or (now.isoformat(timespec="seconds") if now else "")
    horse = getattr(view, "original_primary", "") or (existing or {}).get("pick_name") or ""

    if existing and existing.get("locked"):
        updated_ok = mark_primary_scratched(
            d,
            view.meeting_url,
            int(view.race_no),
            db_path=db_path,
            source=source,
            detected_at=detected,
            active_primary=view.primary,
            active_backup=view.backup,
            backup_scratched=bool(getattr(view, "backup_scratched", False)),
            backup_promoted=bool(getattr(view, "backup_promoted", False)),
            original_backup=getattr(view, "original_backup", "") or "",
        )
        if updated_ok:
            log_late_scratch_transition(
                venue=view.venue_raw or view.venue,
                race_no=view.race_no,
                horse=horse,
                source=source,
                locked=True,
                new_primary=view.primary,
                new_backup=view.backup,
            )
        return get_pick(d, view.meeting_url, int(view.race_no), db_path=db_path)

    # Unlocked: rewrite live autosave with new active names; keep original_* for audit.
    if existing and existing.get("locked"):
        return existing
    orig_p = str((existing or {}).get("original_primary") or (existing or {}).get("pick_name") or view.original_primary or "")
    orig_b = str((existing or {}).get("original_backup") or (existing or {}).get("backup") or view.original_backup or "")
    same = (
        existing
        and str(existing.get("pick_name") or "") == (view.primary or "")
        and str(existing.get("backup") or "") == (view.backup or "")
        and bool(existing.get("primary_scratched")) == bool(view.primary_scratched)
        and str(existing.get("active_primary") or "") == (view.primary or "")
    )
    if same:
        return existing
    if not view.primary:
        # No active selection: flag only, do not write an invalid snapshot name.
        if existing:
            mark_primary_scratched(
                d,
                view.meeting_url,
                int(view.race_no),
                db_path=db_path,
                source=source,
                detected_at=detected,
                active_primary="",
                active_backup="",
                backup_scratched=True,
                original_backup=orig_b,
            )
            log_late_scratch_transition(
                venue=view.venue_raw or view.venue,
                race_no=view.race_no,
                horse=horse,
                source=source,
                locked=False,
                new_primary="",
                new_backup="",
            )
            return get_pick(d, view.meeting_url, int(view.race_no), db_path=db_path)
        return None

    payload = build_snapshot_payload(
        meeting_date=d,
        code=view.code,
        venue=view.venue_raw or view.venue,
        meeting_url=view.meeting_url,
        race_no=view.race_no,
        race_name=view.race_name or f"R{view.race_no}",
        race_url=view.race_url,
        primary=view.primary,
        backup=view.backup,
        primary_score=view.primary_score,
        backup_score=view.backup_score,
        why_bullets=list(view.why or []),
        weights=view.weights,
        field=snapshot_field(view.runners),
        scratching_state=[
            {"name": n, "scratched": True, "sources": srcs, "confirmed_at": detected}
            for n, srcs in (view.scratching_sources or {}).items()
        ],
        primary_odds=view.odds,
        backup_odds=view.backup_odds,
        scheduled_jump=view.jump_at.isoformat() if view.jump_at else "",
        track_condition=view.track_condition,
        status=view.status,
        field_size=view.field_size,
    )
    payload["original_primary"] = orig_p or view.original_primary
    payload["original_backup"] = orig_b or view.original_backup
    save_pick(
        d,
        view.meeting_url,
        view.code,
        int(view.race_no),
        view.venue_raw or view.venue,
        view.race_name or f"R{view.race_no}",
        view.primary,
        backup=view.backup,
        pick_data=payload,
        best_score=view.primary_score,
        backup_score=view.backup_score,
        field_size=view.field_size,
        status=view.status,
        confidence_label=view.confidence_label,
        score_gap=view.score_gap,
        primary_odds=view.odds,
        backup_odds=view.backup_odds,
        original_primary=orig_p or view.original_primary,
        original_backup=orig_b or view.original_backup,
        primary_scratched=bool(view.primary_scratched),
        backup_scratched=bool(view.backup_scratched),
        backup_promoted=bool(view.backup_promoted),
        scratching_source=source,
        scratching_detected_at=detected,
        active_primary=view.primary,
        active_backup=view.backup,
        scheduled_jump=view.jump_at.isoformat() if view.jump_at else "",
        db_path=db_path,
    )
    log_late_scratch_transition(
        venue=view.venue_raw or view.venue,
        race_no=view.race_no,
        horse=horse,
        source=source,
        locked=False,
        new_primary=view.primary,
        new_backup=view.backup,
    )
    return get_pick(d, view.meeting_url, int(view.race_no), db_path=db_path)


def snapshot_field(runners: list, ranked_by_name: Optional[dict[str, Any]] = None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in runners or []:
        name = str(getattr(r, "name", "") or "")
        ranked = (ranked_by_name or {}).get(name)
        out.append(
            {
                "name": name,
                "program_number": program_number_for_runner(r),
                "draw": getattr(r, "draw", None),
                "scratched": bool(getattr(r, "scratched", False)),
                "jockey": getattr(r, "jockey_or_driver", None),
                "trainer": getattr(r, "trainer", None),
                "weight_kg": getattr(r, "weight_kg", None),
                "last10": getattr(r, "last10", None),
                "silk_url": getattr(r, "silk_url", None),
                "score": float(getattr(ranked, "score", 0.0) or 0.0) if ranked is not None else None,
                "rank": getattr(ranked, "rank", None) if ranked is not None else None,
            }
        )
    return out
