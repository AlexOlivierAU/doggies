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
from services.card_loader import fetch_odds_bundle, make_odds_lookup, make_odds_rows_lookup, refresh_card
from services.pick_service import apply_view_scratching, maybe_autolock, save_selection_snapshot, snapshot_field
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
    kind: str = "card"
    chosen_date: date | None = None
    partial: bool = False


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
        if not view.primary or view.no_active_selection:
            continue
        if view.status not in {"upcoming", "in_progress"}:
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
        if view.primary_scratched or view.backup_scratched or view.no_active_selection:
            scratched = apply_view_scratching(view, chosen_date=chosen_date, db_path=db_path, now=now)
            if scratched:
                index[view.race_key] = scratched
    return index


def _fetch_results(code: str, meeting_url: str):
    from review import fetch_results_for_meeting

    return fetch_results_for_meeting(code, meeting_url)


def _lookups(index, by_event):
    if not index:
        return None, None
    return make_odds_lookup(index, by_event), make_odds_rows_lookup(index, by_event)


def _views_for(
    *,
    chosen_date: date,
    meetings: list,
    fields: dict,
    tz_name: str,
    state_filter: str,
    db_path: Path,
    odds_lookup=None,
    odds_rows_lookup=None,
) -> tuple[list[RaceView], datetime]:
    app_tz = resolve_tz(tz_name)
    now = datetime.now(app_tz)
    saved = _picks_index(load_picks(chosen_date, db_path=db_path))
    views = build_race_views(
        chosen_date=chosen_date,
        meetings=meetings,
        fields_by_meeting=fields,
        now=now,
        app_tz=app_tz,
        state_filter=state_filter,
        saved_picks=saved,
        odds_lookup=odds_lookup,
        odds_rows_lookup=odds_rows_lookup,
        rank_upcoming_only=False,
    )
    return views, now


def _merge_odds_maps(base_index: dict, base_by: dict, incoming_index: dict, incoming_by: dict) -> tuple[dict, dict]:
    index = dict(base_index or {})
    if incoming_index:
        index = dict(incoming_index)
    by_event = dict(base_by or {})
    for eid, table in (incoming_by or {}).items():
        if table:
            by_event[eid] = table
    return index, by_event


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
    kind: str = "card",
    previous_odds_index: dict | None = None,
    previous_odds_by_event: dict | None = None,
    on_partial=None,
    cached_at: str = "",
) -> CardBundle:
    def note(msg: str) -> None:
        if progress:
            progress(msg)

    db_path = Path(db_path)
    odds_index = dict(previous_odds_index or {})
    odds_by_event = dict(previous_odds_by_event or {})

    if kind == "odds":
        note("Updating odds")
        from services.card_loader import RefreshPayload

        meetings = list(previous_meetings or [])
        fields = dict(previous_fields or {})
        if not meetings:
            from services.card_loader import load_cached_card

            meetings, fields = load_cached_card(chosen_date, db_path)
        lookup, rows_lookup = _lookups(odds_index, odds_by_event)
        views, now = _views_for(
            chosen_date=chosen_date,
            meetings=meetings,
            fields=fields,
            tz_name=tz_name,
            state_filter=state_filter,
            db_path=db_path,
            odds_lookup=lookup,
            odds_rows_lookup=rows_lookup,
        )
        errors: list[str] = []
        if views:
            new_index, new_by, odds_errors = fetch_odds_bundle(chosen_date, views)
            errors.extend(odds_errors)
            if new_index or new_by:
                odds_index, odds_by_event = _merge_odds_maps(odds_index, odds_by_event, new_index, new_by)
                lookup, rows_lookup = _lookups(odds_index, odds_by_event)
                views, now = _views_for(
                    chosen_date=chosen_date,
                    meetings=meetings,
                    fields=fields,
                    tz_name=tz_name,
                    state_filter=state_filter,
                    db_path=db_path,
                    odds_lookup=lookup,
                    odds_rows_lookup=rows_lookup,
                )
            elif odds_errors:
                errors = ["Fields loaded; odds currently unavailable"]
        payload = RefreshPayload(
            kind="odds",
            status="partial" if errors else "success",
            message=errors[0] if errors else "Odds updated",
            meetings=meetings,
            fields_by_meeting=fields,
            errors=errors,
            chosen_date=chosen_date,
        )
        picks = load_picks(chosen_date, db_path=db_path)
        results = load_results_for_date(chosen_date, db_path=db_path)
        return CardBundle(
            payload=payload,
            views=views,
            picks=picks,
            results=results or {},
            odds_index=odds_index,
            odds_by_event=odds_by_event,
            kind="odds",
            chosen_date=chosen_date,
        )

    if kind == "results":
        note("Checking results")
        from services.card_loader import RefreshPayload

        meetings = list(previous_meetings or [])
        fields = dict(previous_fields or {})
        if not meetings:
            from services.card_loader import load_cached_card

            meetings, fields = load_cached_card(chosen_date, db_path)
        lookup, rows_lookup = _lookups(odds_index, odds_by_event)
        views, now = _views_for(
            chosen_date=chosen_date,
            meetings=meetings,
            fields=fields,
            tz_name=tz_name,
            state_filter=state_filter,
            db_path=db_path,
            odds_lookup=lookup,
            odds_rows_lookup=rows_lookup,
        )
        errors: list[str] = []
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
            errors.append("Result check failed; persisted results kept")
        results = load_results_for_date(chosen_date, db_path=db_path) or {}
        picks = load_picks(chosen_date, db_path=db_path)
        payload = RefreshPayload(
            kind="results",
            status="partial" if errors else "success",
            message=errors[0] if errors else "Results updated",
            meetings=meetings,
            fields_by_meeting=fields,
            errors=errors,
            chosen_date=chosen_date,
        )
        return CardBundle(
            payload=payload,
            views=views,
            picks=picks,
            results=results,
            odds_index=odds_index,
            odds_by_event=odds_by_event,
            kind="results",
            chosen_date=chosen_date,
        )

    live_card = bool(live) and kind in {"card", "all"}
    if kind == "cached":
        note("Loading cached card")
        live_card = False
    else:
        note("Loading meetings")

        def remember(payload) -> None:
            if on_partial:
                on_partial(payload)

    payload = refresh_card(
        chosen_date,
        previous_meetings=previous_meetings,
        previous_fields=previous_fields,
        db_path=db_path,
        live=live_card,
        force=force,
        on_update=remember if kind == "card" else None,
        cached_at=cached_at,
    )
    payload.kind = "cached" if kind == "cached" else payload.kind
    note("Loading fields")
    lookup, rows_lookup = _lookups(odds_index, odds_by_event)
    views, now = _views_for(
        chosen_date=chosen_date,
        meetings=payload.meetings,
        fields=payload.fields_by_meeting,
        tz_name=tz_name,
        state_filter=state_filter,
        db_path=db_path,
        odds_lookup=lookup,
        odds_rows_lookup=rows_lookup,
    )
    if include_odds and views:
        note("Updating odds")
        new_index, new_by, odds_errors = fetch_odds_bundle(chosen_date, views)
        payload.errors.extend(odds_errors)
        if new_index or new_by:
            odds_index, odds_by_event = _merge_odds_maps(odds_index, odds_by_event, new_index, new_by)
            lookup, rows_lookup = _lookups(odds_index, odds_by_event)
            views, now = _views_for(
                chosen_date=chosen_date,
                meetings=payload.meetings,
                fields=payload.fields_by_meeting,
                tz_name=tz_name,
                state_filter=state_filter,
                db_path=db_path,
                odds_lookup=lookup,
                odds_rows_lookup=rows_lookup,
            )
        elif odds_errors:
            payload.errors.append("Fields loaded; odds currently unavailable")
            if payload.status == "success":
                payload.status = "partial"
                payload.message = "Fields loaded; odds currently unavailable"
    if kind != "cached":
        _autosave_unlocked(views, chosen_date, db_path)
        picks_index = _apply_autolock_and_scratches(views, chosen_date, now, db_path)
    else:
        picks_index = _picks_index(load_picks(chosen_date, db_path=db_path))
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
            payload.errors.append("Result check failed; persisted results kept")
            if payload.status == "success":
                payload.status = "partial"
                payload.message = "Result check failed; persisted results kept"
    results = load_results_for_date(chosen_date, db_path=db_path) or {}
    picks = list(picks_index.values()) or load_picks(chosen_date, db_path=db_path)
    if kind == "cached" and views:
        if not payload.message or payload.message in {"Offline/cached data", "Last refresh successful"}:
            payload.message = f"Using cached card from {cached_at}" if cached_at else "Cached — refreshing"
            payload.status = "cached"
            payload.from_cache = True
    elif kind == "cached" and not views:
        payload.status = "cached"
        payload.message = "Loading today's thoroughbred meetings…"
        payload.from_cache = True
    return CardBundle(
        payload=payload,
        views=views,
        picks=picks,
        results=results,
        odds_index=odds_index,
        odds_by_event=odds_by_event,
        kind=kind,
        chosen_date=chosen_date,
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

    def _bundle_from_payload(self, payload, *, kind: str, ctx: dict) -> CardBundle:
        chosen_date: date = ctx["chosen_date"]
        db_path = Path(ctx["db_path"])
        tz_name = str(ctx.get("tz_name") or "Australia/Sydney")
        state_filter = str(ctx.get("state_filter") or "All")
        prev_odds = dict(ctx.get("odds_index") or {})
        prev_by = dict(ctx.get("odds_by_event") or {})
        lookup, rows_lookup = _lookups(prev_odds, prev_by)
        views, _now = _views_for(
            chosen_date=chosen_date,
            meetings=payload.meetings,
            fields=payload.fields_by_meeting,
            tz_name=tz_name,
            state_filter=state_filter,
            db_path=db_path,
            odds_lookup=lookup,
            odds_rows_lookup=rows_lookup,
        )
        picks = load_picks(chosen_date, db_path=db_path)
        results = load_results_for_date(chosen_date, db_path=db_path) or {}
        return CardBundle(
            payload=payload,
            views=views,
            picks=picks,
            results=results,
            odds_index=prev_odds,
            odds_by_event=prev_by,
            kind=kind,
            chosen_date=chosen_date,
        )

    @Slot(str, object)
    def run(self, kind: str, ctx: object) -> None:
        if not self._alive:
            self.finished_kind.emit(kind)
            return
        data = dict(ctx or {})
        chosen_date: date = data["chosen_date"]
        db_path = Path(data["db_path"])
        try:
            log.info(
                "Worker %s start date=%s db=%s live=%s",
                kind,
                chosen_date,
                db_path.resolve(),
                data.get("live", True),
            )

            def on_partial(payload) -> None:
                if not self._alive:
                    return
                bundle = self._bundle_from_payload(payload, kind=kind, ctx=data)
                bundle.partial = True
                if bundle.views:
                    log.info(
                        "Worker %s partial views=%s meetings=%s",
                        kind,
                        len(bundle.views),
                        len(payload.meetings or []),
                    )
                    self.bundle_ready.emit(bundle)
                    if payload.message:
                        self.progress.emit(payload.message)

            include_odds = kind in {"odds", "all"}
            include_results = kind in {"results", "all"}
            live = bool(data.get("live", True)) and kind in {"card", "all"}
            bundle = build_bundle(
                chosen_date=chosen_date,
                tz_name=str(data.get("tz_name") or "Australia/Sydney"),
                state_filter=str(data.get("state_filter") or "All"),
                db_path=db_path,
                live=live,
                force=bool(data.get("force", False)),
                previous_meetings=list(data.get("meetings") or []),
                previous_fields=dict(data.get("fields") or {}),
                include_odds=include_odds,
                include_results=include_results,
                progress=self.progress.emit if self._alive else None,
                kind=kind,
                previous_odds_index=dict(data.get("odds_index") or {}),
                previous_odds_by_event=dict(data.get("odds_by_event") or {}),
                on_partial=on_partial if kind == "card" else None,
                cached_at=str(data.get("cached_at") or ""),
            )
            bundle.kind = kind
            bundle.chosen_date = chosen_date
            bundle.payload.kind = kind
            if self._alive:
                log.info(
                    "Worker %s done views=%s meetings=%s status=%s",
                    kind,
                    len(bundle.views),
                    len(getattr(bundle.payload, "meetings", None) or []),
                    getattr(bundle.payload, "status", ""),
                )
                self.bundle_ready.emit(bundle)
        except Exception as exc:
            log.exception("Worker %s failed", kind)
            from desktop.status import safe_error_summary

            if self._alive:
                self.failed.emit(kind, safe_error_summary(exc, kind=kind, db_path=db_path))
        finally:
            if self._alive:
                self.finished_kind.emit(kind)
