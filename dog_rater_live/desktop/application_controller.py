"""Owns data, workers and timers. Widgets bind to signals only."""

from __future__ import annotations

import logging
from datetime import date, datetime

from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot

from desktop.notifications import NotificationService
from desktop.refresh_gate import RefreshGate
from desktop.settings import DesktopSettings
from desktop.workers.refresh_worker import CardBundle, RefreshWorker
from services.card_loader import make_odds_lookup, merge_fields_maps
from services.pick_service import build_snapshot_payload, save_selection_snapshot, snapshot_field
from services.race_day_service import RaceView, build_race_views, live_status, next_to_jump, resolve_tz, upcoming_races

log = logging.getLogger("race_day_rater.controller")


class ApplicationController(QObject):
    status_changed = Signal(str)
    health_changed = Signal(str, str)
    views_changed = Signal()
    clock_ticked = Signal()
    notify = Signal(str, str)
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
        self.selected_key = None
        self.last_ok = ""
        self.health = "idle"
        self._alive = True
        self._stopped = False
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
        return datetime.now(self.app_tz)

    def odds_lookup(self):
        if not self.odds_index:
            return None
        return make_odds_lookup(self.odds_index, self.odds_by_event)

    def start(self) -> None:
        self.clock_timer.start()
        self._sync_auto_timers()
        self.status_changed.emit("Loading meetings")
        self.request_refresh("card", live=True, force=False)

    def _sync_auto_timers(self) -> None:
        for t in (self.odds_timer, self.fields_timer, self.results_timer):
            t.stop()
        if not self.settings.auto_refresh:
            return
        self.odds_timer.start(self.settings.interval_odds_sec * 1000)
        self.fields_timer.start(self.settings.interval_fields_sec * 1000)
        self.results_timer.start(self.settings.interval_results_sec * 1000)

    def apply_settings(self) -> None:
        self._sync_auto_timers()
        self._rebuild_views_local()
        self.views_changed.emit()

    def set_date(self, d: date) -> None:
        self.chosen_date = d
        self.request_refresh("card", live=True, force=False)

    def set_state(self, state: str) -> None:
        self.settings.state_filter = state
        self._rebuild_views_local()
        self.views_changed.emit()

    def request_refresh(self, kind: str = "card", *, live: bool = True, force: bool = False) -> None:
        if not self._alive:
            return
        if not self._gate.request(kind):
            return
        ctx = {
            "chosen_date": self.chosen_date,
            "tz_name": self.settings.timezone,
            "state_filter": self.settings.state_filter,
            "db_path": str(self.settings.db_path),
            "live": live,
            "force": force,
            "meetings": self.meetings,
            "fields": self.fields,
        }
        self._run_worker.emit(kind, ctx)

    def upcoming(self) -> list[RaceView]:
        return upcoming_races(self.views, self.now(), limit=12)

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
        if view is None or not view.primary:
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

    def _rebuild_views_local(self) -> None:
        now = self.now()
        lookup = self.odds_lookup()
        from race_db import load_picks

        picks = load_picks(self.chosen_date, db_path=self.settings.db_path)
        index = {}
        for p in picks:
            try:
                index[(str(p.get("meeting_url") or ""), int(p.get("race_no") or 0))] = p
            except Exception:
                continue
        self.views = build_race_views(
            chosen_date=self.chosen_date,
            meetings=self.meetings,
            fields_by_meeting=self.fields,
            now=now,
            app_tz=self.app_tz,
            state_filter=self.settings.state_filter,
            saved_picks=index,
            odds_lookup=lookup,
            rank_upcoming_only=False,
        )
        self.hero = next_to_jump(self.views, now)
        self.picks = picks

    @Slot(str)
    def _on_progress(self, msg: str) -> None:
        if self._alive:
            self.status_changed.emit(msg)

    @Slot(object)
    def _on_bundle(self, bundle: object) -> None:
        if not self._alive or not isinstance(bundle, CardBundle):
            return
        payload = bundle.payload
        if payload.status == "failure":
            self.health = "error"
            self.health_changed.emit(self.health, self.last_ok)
            self.status_changed.emit(payload.message or "Refresh failed")
            if not self.views:
                self.meetings = payload.meetings
                self.fields = payload.fields_by_meeting
                self.views = bundle.views
                self.hero = next_to_jump(self.views, self.now())
                self.views_changed.emit()
            return
        if payload.meetings:
            self.meetings = payload.meetings
        if payload.fields_by_meeting:
            self.fields = merge_fields_maps(self.fields, payload.fields_by_meeting)
        if bundle.views:
            self.views = bundle.views
        if bundle.picks:
            self.picks = bundle.picks
        if bundle.results is not None:
            self.results = bundle.results
        if bundle.odds_index:
            self.odds_index = bundle.odds_index
            self.odds_by_event = bundle.odds_by_event
        self.hero = next_to_jump(self.views, self.now())
        if payload.status == "success":
            self.health = "ok"
            self.last_ok = self.now().strftime("%H:%M:%S")
        elif payload.status == "partial":
            self.health = "warn"
        else:
            self.health = "cached"
            if not self.last_ok:
                self.last_ok = self.now().strftime("%H:%M:%S")
        self.health_changed.emit(self.health, self.last_ok)
        self.status_changed.emit(payload.message)
        self.views_changed.emit()
        self._emit_notifications()

    @Slot(str, str)
    def _on_failed(self, _kind: str, message: str) -> None:
        if not self._alive:
            return
        self.health = "error" if not self.views else "warn"
        self.health_changed.emit(self.health, self.last_ok)
        self.status_changed.emit(message)

    @Slot(str)
    def _on_finished(self, _kind: str) -> None:
        follow = self._gate.finish()
        if follow and self._alive:
            self.request_refresh(follow)

    def _tick(self) -> None:
        if not self._alive:
            return
        now = self.now()
        for v in self.views:
            v.status = live_status(now, v.jump_at)
        self.hero = next_to_jump(self.views, now)
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
