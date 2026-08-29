"""Background card / odds / results work. Never touch QWidgets here."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Signal, Slot

from race_db import (
    load_picks,
    load_results,
    load_results_for_date,
    persist_result_failure,
    persist_results,
)
from services.card_loader import fetch_odds_bundle, make_odds_lookup, refresh_card
from services.pick_service import maybe_autolock, record_primary_scratching, save_selection_snapshot, snapshot_field
from services.race_day_service import RaceView, build_race_views, resolve_tz
from services.result_service import sync_missing_results

log = logging.getLogger("race_day_rater.worker")


@dataclass
class CardBundle:
    payload: object
    views: list[RaceView] = field(default_factory=list)
    picks: list[dict[str, Any]] = field(default_factory=list)
    results: dict = field(default_factory=dict)
    odds_index: dict = field(default_factory=dict)
    odds_by_event: dict = field(default_factory=dict)


def _picks_index(picks: list) -> dict[tuple[str, int], dict]:
    out: dict[tuple[str, int], dict] = {}
    for p in picks or []:
        try:
            out[(str(p.get("meeting_url") or ""), int(p.get("race_no") or 0))] = p
        except Exception:
            continue
    return out


def _autosave_unlocked(views: list[RaceView], chosen_date: date, db_path: Path) -> None:
    from services.pick_service import build_snapshot_payload

    for view in views:
        if not view.primary or view.status not in {"upcoming", "in_progress"}:
            continue
        if view.from_snapshot and view.locked:
            continue
        ranked_by_name = {getattr(r, "name", ""): r for r in (view.ranked or [])}
        payload = build_snapshot_payload(
            meeting_date=chosen_date,
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
            field=snapshot_field(view.runners, ranked_by_name),
            primary_odds=view.odds,
            backup_odds=view.backup_odds,
            primary_number=view.primary_no,
            backup_number=view.backup_no,
            scheduled_jump=view.jump_at.isoformat() if view.jump_at else "",
            track_condition=view.track_condition,
            status=view.status,
            field_size=view.field_size,
            why_bullets=view.why,
            weights=view.weights,
        )
        try:
            save_selection_snapshot(
                meeting_date=chosen_date,
                meeting_url=view.meeting_url,
                code=view.code,
                race_no=view.race_no,
                venue=view.venue_raw or view.venue,
                race_label=f"R{view.race_no}",
                primary=view.primary,
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
                scheduled_jump=view.jump_at.isoformat() if view.jump_at else "",
                lock=False,
                db_path=db_path,
            )
        except Exception:
            log.exception("Unlocked autosave failed for %s R%s", view.venue, view.race_no)


def _apply_autolock_and_scratches(views: list[RaceView], chosen_date: date, now: datetime, db_path: Path) -> dict:
    picks = load_picks(chosen_date, db_path=db_path)
    index = _picks_index(picks)
    for view in views:
        saved = index.get(view.race_key)
        if not saved:
            continue
        updated = maybe_autolock(saved, now=now, jump_at=view.jump_at, db_path=db_path)
        index[view.race_key] = updated
        if view.scratching_warning and not updated.get("primary_scratched"):
            scratched = record_primary_scratching(chosen_date, view.meeting_url, view.race_no, db_path=db_path)
            if scratched:
                index[view.race_key] = scratched
    return index


def _fetch_results(code: str, meeting_url: str):
    from review import fetch_results_for_meeting

    return fetch_results_for_meeting(code, meeting_url)


def build_bundle(
    *,
    chosen_date: date,
    tz_name: str,
    state_filter: str,
    db_path: Path,
    live: bool,
    force: bool,
    previous_meetings: list,
    previous_fields: dict,
    include_odds: bool,
    include_results: bool,
    progress=None,
) -> CardBundle:
    def note(msg: str) -> None:
        if progress:
            progress(msg)

    note("Loading meetings")
    payload = refresh_card(
        chosen_date,
        previous_meetings=previous_meetings,
        previous_fields=previous_fields,
        db_path=db_path,
        live=live,
        force=force,
    )
    app_tz = resolve_tz(tz_name)
    now = datetime.now(app_tz)
    note("Loading fields")
    saved = _picks_index(load_picks(chosen_date, db_path=db_path))
    views = build_race_views(
        chosen_date=chosen_date,
        meetings=payload.meetings,
        fields_by_meeting=payload.fields_by_meeting,
        now=now,
        app_tz=app_tz,
        state_filter=state_filter,
        saved_picks=saved,
        rank_upcoming_only=False,
    )
    odds_index: dict = {}
    odds_by_event: dict = {}
    if include_odds and views:
        note("Updating odds")
        odds_index, odds_by_event, odds_errors = fetch_odds_bundle(chosen_date, views)
        payload.errors.extend(odds_errors)
        if odds_errors and payload.status == "success":
            payload.status = "partial"
            payload.message = "Partial source failure"
        lookup = make_odds_lookup(odds_index, odds_by_event)
        views = build_race_views(
            chosen_date=chosen_date,
            meetings=payload.meetings,
            fields_by_meeting=payload.fields_by_meeting,
            now=now,
            app_tz=app_tz,
            state_filter=state_filter,
            saved_picks=saved,
            odds_lookup=lookup,
            rank_upcoming_only=False,
        )
    _autosave_unlocked(views, chosen_date, db_path)
    picks_index = _apply_autolock_and_scratches(views, chosen_date, now, db_path)
    if include_results:
        note("Checking results")
        try:
            sync_missing_results(
                chosen_date=chosen_date,
                views=views,
                now=now,
                fetch_meeting_results=_fetch_results,
                load_results_fn=load_results,
                persist_results_fn=persist_results,
                persist_failure_fn=persist_result_failure,
                db_path=db_path,
            )
        except Exception:
            log.exception("Result sync failed")
            payload.errors.append("Result check failed.")
            if payload.status == "success":
                payload.status = "partial"
                payload.message = "Partial source failure"
    results = load_results_for_date(chosen_date, db_path=db_path)
    picks = list(picks_index.values()) or load_picks(chosen_date, db_path=db_path)
    return CardBundle(
        payload=payload,
        views=views,
        picks=picks,
        results=results,
        odds_index=odds_index,
        odds_by_event=odds_by_event,
    )


class RefreshWorker(QObject):
    progress = Signal(str)
    bundle_ready = Signal(object)
    failed = Signal(str, str)
    finished_kind = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self._alive = True

    @Slot()
    def stop(self) -> None:
        self._alive = False

    @Slot(str, object)
    def run(self, kind: str, ctx: object) -> None:
        if not self._alive:
            self.finished_kind.emit(kind)
            return
        try:
            data = dict(ctx or {})
            chosen_date: date = data["chosen_date"]
            bundle = build_bundle(
                chosen_date=chosen_date,
                tz_name=str(data.get("tz_name") or "Australia/Sydney"),
                state_filter=str(data.get("state_filter") or "All"),
                db_path=Path(data["db_path"]),
                live=bool(data.get("live", True)) and kind in {"card", "all"},
                force=bool(data.get("force", False)),
                previous_meetings=list(data.get("meetings") or []),
                previous_fields=dict(data.get("fields") or {}),
                include_odds=kind in {"odds", "all", "card"},
                include_results=kind in {"results", "all", "card"},
                progress=self.progress.emit if self._alive else None,
            )
            if kind == "odds":
                bundle.payload.kind = "odds"
            elif kind == "results":
                bundle.payload.kind = "results"
            if self._alive:
                self.bundle_ready.emit(bundle)
        except Exception:
            log.exception("Worker %s failed", kind)
            if self._alive:
                self.failed.emit(kind, "Refresh failed")
        finally:
            if self._alive:
                self.finished_kind.emit(kind)
