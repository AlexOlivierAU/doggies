"""Owns data, workers and timers. Widgets bind to signals only."""

from __future__ import annotations

import logging
from datetime import date, datetime
from pathlib import Path

from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot

from desktop.notifications import NotificationService
from desktop.paths import desktop_log_path, shared_default_db_path
from desktop.refresh_gate import RefreshGate
from desktop.settings import DesktopSettings
from desktop.status import (
    CARD_READY,
    CHECKING_RESULTS,
    EMPTY,
    ENRICHING_ODDS,
    ERROR,
    LOADING_CACHE,
    LOADING_CARD,
    LOADING_MESSAGE,
    OFFLINE_CACHED,
    PARTIAL,
    STARTING,
    STAGE_STATUS,
    is_loading,
    safe_error_summary,
)
from desktop.workers.refresh_worker import CardBundle, RefreshWorker
from race_db import load_picks, load_daily_fields, load_daily_meetings
from services.card_loader import MEETINGS_CODE, make_odds_lookup, make_odds_rows_lookup, merge_fields_maps
from services.pick_service import build_snapshot_payload, save_selection_snapshot, snapshot_field
from services.race_day_service import (
    RaceDayState,
    RaceView,
    build_race_views,
    derive_race_day_state,
    live_status,
    resolve_tz,
)

log = logging.getLogger("race_day_rater.controller")


def _hero_label(view: RaceView | None) -> str:
    if view is None:
        return "none"
    venue = view.venue_raw or view.venue or "?"
    clock = view.clock() if view.jump_at else "—"
    return f"{venue} R{view.race_no} {clock}"


def _preserve_odds(old_views: list[RaceView], new_views: list[RaceView]) -> list[RaceView]:
    prev = {v.race_key: v for v in old_views or []}
    for view in new_views or []:
        prior = prev.get(view.race_key)
        if prior is None:
            continue
        if view.odds is None and prior.odds is not None:
            view.odds = prior.odds
        if view.backup_odds is None and prior.backup_odds is not None:
            view.backup_odds = prior.backup_odds
    return new_views


class ApplicationController(QObject):
    status_changed = Signal(str)
    health_changed = Signal(str, str)
    views_changed = Signal()
    clock_ticked = Signal()
    notify = Signal(str, str)
    refresh_busy_changed = Signal(bool)
    stage_changed = Signal(str)
    _run_worker = Signal(str, object)

    def __init__(self, settings: DesktopSettings | None = None, parent=None) -> None:
        super().__init__(parent)
        self.settings = settings or DesktopSettings()
        self.chosen_date = date.today()
        self.meetings: list = []
        self.fields: dict = {}
        self.views: list[RaceView] = []
        self.picks: list = []
        self.results: dict = {}
        self.odds_index: dict = {}
        self.odds_by_event: dict = {}
        self.hero: RaceView | None = None
        self._derived: RaceDayState | None = None
        self._frozen_now: datetime | None = None
        self.selected_key = None
        self.last_ok = ""
        self.health = "idle"
        self.stage = STARTING
        self.last_error = ""
        self.cached_at = ""
        self._alive = True
        self._stopped = False
        self._card_busy = False
        self._gate = RefreshGate()
        self._notifs = NotificationService(self.settings.notified_ids(), self.settings.add_notified)

        self._thread = QThread(self)
        self._worker = RefreshWorker()
        self._worker.moveToThread(self._thread)
        self._run_worker.connect(self._worker.run)
        self._worker.progress.connect(self._on_progress)
        self._worker.bundle_ready.connect(self._on_bundle)
        self._worker.failed.connect(self._on_failed)
        self._worker.finished_kind.connect(self._on_finished)
        self._thread.start()

        self.clock_timer = QTimer(self)
        self.clock_timer.setInterval(1000)
        self.clock_timer.timeout.connect(self._tick)

        self.odds_timer = QTimer(self)
        self.odds_timer.timeout.connect(lambda: self.request_refresh("odds"))
        self.fields_timer = QTimer(self)
        self.fields_timer.timeout.connect(lambda: self.request_refresh("card"))
        self.results_timer = QTimer(self)
        self.results_timer.timeout.connect(lambda: self.request_refresh("results"))

    @property
    def app_tz(self):
        return resolve_tz(self.settings.timezone)

    def now(self) -> datetime:
        if self._frozen_now is not None:
            dt = self._frozen_now
            if dt.tzinfo is None:
                return dt.replace(tzinfo=self.app_tz)
            return dt.astimezone(self.app_tz)
        return datetime.now(self.app_tz)

    def freeze_now(self, when: datetime | None) -> None:
        """Test helper: pin `now()` to a timezone-aware instant."""
        self._frozen_now = when

    def race_day_state(self, now: datetime | None = None) -> RaceDayState:
        when = now if now is not None else self.now()
        if when.tzinfo is None:
            when = when.replace(tzinfo=self.app_tz)
        else:
            when = when.astimezone(self.app_tz)
        return derive_race_day_state(self.views, when, sticky=self.hero, limit=12)

    def sync_race_day(self, now: datetime | None = None) -> RaceDayState:
        state = self.race_day_state(now)
        self._commit_hero(state)
        self._derived = state
        return state

    @property
    def last_race_day_state(self) -> RaceDayState | None:
        return self._derived

    def _commit_hero(self, state: RaceDayState) -> None:
        old = self.hero
        new = state.hero
        old_key = old.race_key if old is not None else None
        new_key = new.race_key if new is not None else None
        if old_key != new_key:
            log.debug(
                "Hero transition: %s → %s old_key=%s new_key=%s old_jump=%s new_jump=%s now=%s tz=%s",
                _hero_label(old),
                _hero_label(new),
                old_key,
                new_key,
                getattr(old, "jump_at", None) if old is not None else None,
                getattr(new, "jump_at", None) if new is not None else None,
                state.now.isoformat(),
                str(self.app_tz),
            )
        self.hero = new

    def odds_lookup(self):
        if not self.odds_index:
            return None
        return make_odds_lookup(self.odds_index, self.odds_by_event)

    def odds_rows_lookup(self):
        if not self.odds_index:
            return None
        return make_odds_rows_lookup(self.odds_index, self.odds_by_event)

    def _set_stage(self, stage: str, status: str | None = None) -> None:
        self.stage = stage
        self.stage_changed.emit(stage)
        self.status_changed.emit(status if status is not None else STAGE_STATUS.get(stage, stage))

    def start(self) -> None:
        db = self.settings.db_path
        log.info("Database: %s exists=%s", db.resolve() if db.exists() else db, db.exists())
        if self.settings.db_path_warning:
            log.warning("%s", self.settings.db_path_warning)
        self.clock_timer.start()
        self._sync_auto_timers()
        self._set_stage(LOADING_CACHE, LOADING_MESSAGE)
        self.views_changed.emit()
        self.request_refresh("cached", live=False, force=False)
        self.request_refresh("card", live=True, force=False)

    def _sync_auto_timers(self) -> None:
        for t in (self.odds_timer, self.fields_timer, self.results_timer):
            t.stop()
        if not self.settings.auto_refresh:
            return
        self.odds_timer.start(self.settings.interval_odds_sec * 1000)
        self.fields_timer.start(self.settings.interval_fields_sec * 1000)
        self.results_timer.start(self.settings.interval_results_sec * 1000)

    def set_auto_refresh(self, on: bool) -> None:
        self.settings.auto_refresh = on
        self._sync_auto_timers()

    def apply_settings(self) -> None:
        self._sync_auto_timers()
        self._rebuild_views_local()
        self.views_changed.emit()

    def set_date(self, d: date) -> None:
        if d == self.chosen_date:
            return
        self.chosen_date = d
        self.meetings = []
        self.fields = {}
        self.views = []
        self.hero = None
        self._derived = None
        self.odds_index = {}
        self.odds_by_event = {}
        self.picks = []
        self.results = {}
        self.cached_at = ""
        self._set_stage(LOADING_CACHE, LOADING_MESSAGE)
        self.views_changed.emit()
        self.request_refresh("cached", live=False, force=False)
        self.request_refresh("card", live=True, force=False)

    def set_state(self, state: str) -> None:
        self.settings.state_filter = state
        self._rebuild_views_local()
        self.views_changed.emit()

    def request_manual_refresh(self) -> None:
        if self._card_busy:
            return
        self.request_refresh("card", live=True, force=True)

    def request_refresh(self, kind: str = "card", *, live: bool = True, force: bool = False) -> None:
        if not self._alive:
            return
        if not self._gate.request(kind):
            return
        if kind in {"card", "cached"}:
            self._card_busy = True
            self.refresh_busy_changed.emit(True)
            if kind == "card" and not self.views:
                self._set_stage(LOADING_CARD, LOADING_MESSAGE)
            elif kind == "cached" and not self.views:
                self._set_stage(LOADING_CACHE, LOADING_MESSAGE)
        ctx = {
            "chosen_date": self.chosen_date,
            "tz_name": self.settings.timezone,
            "state_filter": self.settings.state_filter,
            "db_path": str(self.settings.db_path),
            "live": live,
            "force": force,
            "meetings": self.meetings,
            "fields": self.fields,
            "odds_index": self.odds_index,
            "odds_by_event": self.odds_by_event,
            "cached_at": self.cached_at or self.now().strftime("%H:%M"),
        }
        self._run_worker.emit(kind, ctx)

    def upcoming(self) -> list[RaceView]:
        if self._derived is not None:
            return self._derived.upcoming
        return self.race_day_state().upcoming

    def view_for_key(self, key) -> RaceView | None:
        if key is None:
            return None
        if isinstance(key, RaceView):
            return key
        for v in self.views:
            if v.race_key == key:
                return v
        return None

    def select_race(self, key) -> RaceView | None:
        view = self.view_for_key(key)
        if view is not None:
            self.selected_key = view.race_key
        return view

    def lock_view(self, view: RaceView | None) -> bool:
        if view is None or not view.primary or getattr(view, "no_active_selection", False):
            return False
        ranked_by_name = {getattr(r, "name", ""): r for r in (view.ranked or [])}
        payload = build_snapshot_payload(
            meeting_date=self.chosen_date,
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
        ok = save_selection_snapshot(
            meeting_date=self.chosen_date,
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
            lock=True,
            db_path=self.settings.db_path,
        )
        if ok:
            self.request_refresh("results", live=False)
        return ok

    def diagnostics(self) -> dict:
        db = self.settings.db_path
        meetings = load_daily_meetings(self.chosen_date, MEETINGS_CODE, db_path=db) or []
        field_count = 0
        for m in meetings:
            url = getattr(m, "meeting_url", "") or ""
            if url and load_daily_fields(self.chosen_date, url, db_path=db):
                field_count += 1
        return {
            "db_path": str(db.resolve() if db.exists() else Path(db).expanduser()),
            "db_exists": db.exists(),
            "db_warning": self.settings.db_path_warning,
            "obsolete_db_path": self.settings.obsolete_db_path,
            "default_db_path": str(shared_default_db_path()),
            "cached_meetings": len(meetings),
            "cached_fields": field_count,
            "stage": self.stage,
            "last_error": self.last_error,
            "log_path": str(desktop_log_path()),
        }

    def _rebuild_views_local(self) -> None:
        now = self.now()
        lookup = self.odds_lookup()
        rows_lookup = self.odds_rows_lookup()
        picks = load_picks(self.chosen_date, db_path=self.settings.db_path)
        index = {}
        for p in picks:
            try:
                index[(str(p.get("meeting_url") or ""), int(p.get("race_no") or 0))] = p
            except Exception:
                continue
        new_views = build_race_views(
            chosen_date=self.chosen_date,
            meetings=self.meetings,
            fields_by_meeting=self.fields,
            now=now,
            app_tz=self.app_tz,
            state_filter=self.settings.state_filter,
            saved_picks=index,
            odds_lookup=lookup,
            odds_rows_lookup=rows_lookup,
            rank_upcoming_only=False,
        )
        self.views = _preserve_odds(self.views, new_views)
        from services.pick_service import apply_view_scratching

        for v in self.views:
            if v.primary_scratched or v.backup_scratched or v.no_active_selection:
                apply_view_scratching(v, chosen_date=self.chosen_date, db_path=self.settings.db_path, now=now)
        self.sync_race_day(now)
        self.picks = load_picks(self.chosen_date, db_path=self.settings.db_path)

    @Slot(str)
    def _on_progress(self, msg: str) -> None:
        if self._alive:
            self.status_changed.emit(msg)

    @Slot(object)
    def _on_bundle(self, bundle: object) -> None:
        if not self._alive or not isinstance(bundle, CardBundle):
            return
        payload = bundle.payload
        kind = bundle.kind or getattr(payload, "kind", "card")
        bundle_date = bundle.chosen_date or getattr(payload, "chosen_date", None)
        if bundle_date is not None and bundle_date != self.chosen_date:
            log.info("Ignoring stale %s bundle for %s (current %s)", kind, bundle_date, self.chosen_date)
            return
        if kind == "cached" and not bundle.views and not payload.meetings:
            self._set_stage(LOADING_CARD, LOADING_MESSAGE)
            self.views_changed.emit()
            return
        if payload.status == "failure" and not bundle.views and not payload.meetings:
            if kind == "cached":
                self._set_stage(LOADING_CARD, LOADING_MESSAGE)
                self.views_changed.emit()
                return
            self.last_error = payload.message or "Refresh failed"
            if not self.views:
                self.health = "error"
                self._set_stage(ERROR, self.last_error)
                self.health_changed.emit(self.health, self.last_ok)
            else:
                self.health = "warn"
                self.health_changed.emit(self.health, self.last_ok)
                self.status_changed.emit(self.last_error)
            if not self.views:
                self.views_changed.emit()
            return
        if payload.meetings:
            self.meetings = payload.meetings
        if payload.fields_by_meeting:
            self.fields = merge_fields_maps(self.fields, payload.fields_by_meeting)
        incoming_views = list(bundle.views or [])
        if incoming_views:
            self.views = _preserve_odds(self.views, incoming_views)
        if bundle.picks:
            self.picks = bundle.picks
        if bundle.results:
            self.results = bundle.results
        if bundle.odds_index:
            self.odds_index = bundle.odds_index
        if bundle.odds_by_event:
            merged_odds = dict(self.odds_by_event)
            for eid, table in bundle.odds_by_event.items():
                if table:
                    merged_odds[eid] = table
            self.odds_by_event = merged_odds
        self.sync_race_day()
        errors = list(getattr(payload, "errors", None) or [])
        failed_states = list(getattr(payload, "failed_states", None) or [])
        if errors or failed_states:
            self.last_error = payload.message or (errors[0] if errors else "")
        if kind == "cached" and self.views:
            self.cached_at = self.now().strftime("%H:%M")
            self.health = "cached"
            self._set_stage(OFFLINE_CACHED, "Cached — refreshing")
            if not self.last_ok:
                self.last_ok = self.now().strftime("%H:%M:%S")
        elif kind in {"card", "cached"}:
            if payload.status == "success":
                self.health = "ok"
                self.last_ok = self.now().strftime("%H:%M:%S")
                self._set_stage(CARD_READY, payload.message)
            elif payload.status == "partial":
                self.health = "warn"
                self._set_stage(PARTIAL, payload.message)
            elif payload.status == "cached":
                self.health = "cached"
                self._set_stage(OFFLINE_CACHED, payload.message or "Cached — refreshing")
                if not self.last_ok:
                    self.last_ok = self.now().strftime("%H:%M:%S")
            else:
                self.health = "warn" if self.views else "error"
                self._set_stage(PARTIAL if self.views else ERROR, payload.message)
            if self.views and kind == "card" and payload.status in {"success", "partial"}:
                if not self.cached_at:
                    self.cached_at = self.now().strftime("%H:%M")
        elif kind == "odds":
            self._set_stage(ENRICHING_ODDS if errors else CARD_READY, payload.message)
            if errors:
                self.health = "warn"
            elif self.health == "idle":
                self.health = "ok"
        elif kind == "results":
            self._set_stage(CHECKING_RESULTS if not errors else PARTIAL, payload.message)
            if self.picks and not errors:
                self._rebuild_views_local()
        if not self.views and not is_loading(self.stage) and kind == "card":
            self._set_stage(EMPTY, "No meetings found for selected date")
            self.health = "error"
        self.health_changed.emit(self.health, self.last_ok)
        self.status_changed.emit(payload.message or STAGE_STATUS.get(self.stage, ""))
        self.views_changed.emit()
        if not bundle.partial:
            self._emit_notifications()

    @Slot(str, str)
    def _on_failed(self, kind: str, message: str) -> None:
        if not self._alive:
            return
        summary = message or safe_error_summary(RuntimeError("Refresh failed"), kind=kind, db_path=self.settings.db_path)
        self.last_error = summary
        self.health = "error" if not self.views else "warn"
        self.health_changed.emit(self.health, self.last_ok)
        if not self.views and kind in {"card", "cached"}:
            self._set_stage(ERROR, summary)
        else:
            self.status_changed.emit(summary)
        self.views_changed.emit()

    @Slot(str)
    def _on_finished(self, kind: str) -> None:
        if kind == "card":
            self._card_busy = False
            self.refresh_busy_changed.emit(False)
        follow = self._gate.finish()
        if not self._alive:
            return
        if kind == "cached":
            if follow:
                self.request_refresh(follow, live=follow in {"card", "all"}, force=False)
            else:
                self._card_busy = False
                self.refresh_busy_changed.emit(False)
            return
        if kind == "card":
            if follow == "card":
                self.request_refresh("card", live=True, force=False)
                return
            self.request_refresh("odds")
            self.request_refresh("results")
            if follow and follow not in {"odds", "results", "all"}:
                self.request_refresh(follow)
            elif follow == "all":
                self.request_refresh("odds")
                self.request_refresh("results")
            return
        if follow:
            self.request_refresh(follow)

    def _tick(self) -> None:
        if not self._alive:
            return
        now = self.now()
        for v in self.views:
            v.status = live_status(now, v.jump_at)
        self.sync_race_day(now)
        self.clock_ticked.emit()

    def _emit_notifications(self) -> None:
        from desktop.models.picks_table_model import pick_rows_from_views

        index = {}
        for p in self.picks:
            try:
                index[(str(p.get("meeting_url") or ""), int(p.get("race_no") or 0))] = p
            except Exception:
                continue
        rows, _ = pick_rows_from_views(self.views, index, self.results, self.now())
        for title, body in self._notifs.evaluate(
            views=self.views, picks_rows=rows, now=self.now(), enabled=self.settings.notifications
        ):
            self.notify.emit(title, body)

    def shutdown(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        self._alive = False
        for t in (self.clock_timer, self.odds_timer, self.fields_timer, self.results_timer):
            t.stop()
        try:
            self._worker.stop()
        except Exception:
            pass
        self._thread.quit()
        if not self._thread.wait(4000):
            log.warning("Worker thread did not stop cleanly")
            self._thread.terminate()
            self._thread.wait(1000)
