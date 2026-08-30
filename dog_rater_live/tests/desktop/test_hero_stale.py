"""Stale Next-to-Jump hero card: controller selection must bind the visible widget."""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings

from desktop.application_controller import ApplicationController
from desktop.main_window import MainWindow
from desktop.settings import DesktopSettings
from desktop.workers.refresh_worker import CardBundle
from models import Meeting
from services.card_loader import RefreshPayload
from services.formatting import hero_running_hold
from services.race_day_service import RaceView, jump_datetime

SYD = ZoneInfo("Australia/Sydney")
D = date(2026, 8, 30)


def _rv(
    *,
    url: str,
    venue: str,
    raw: str,
    jump: datetime,
    state: str = "NSW",
    no: int = 1,
    status: str = "upcoming",
) -> RaceView:
    assert jump.tzinfo is not None
    return RaceView(
        meeting_url=url,
        race_url=url + "/r1",
        code="thoroughbred",
        venue=venue,
        venue_raw=raw,
        state=state,
        race_no=no,
        race_name=f"R{no}",
        race_class="",
        distance_m=1200,
        track_condition="Good4",
        jump_at=jump,
        status=status,
        primary="Alpha",
        primary_no="3",
        backup="Beta",
        backup_no="8",
        primary_score=0.5,
        backup_score=0.4,
        score_gap=0.1,
        confidence_label="Medium",
        odds=None,
        backup_odds=None,
        field_size=8,
        scratching_warning=False,
        locked=False,
        from_snapshot=False,
        live_status=status,
    )


def _wyong() -> RaceView:
    return _rv(
        url="https://example.test/wyong",
        venue="Wyong (NSW)",
        raw="Wyong",
        jump=datetime(2026, 8, 30, 12, 35, tzinfo=SYD),
    )


def _casterton() -> RaceView:
    return _rv(
        url="https://example.test/casterton",
        venue="Casterton (VIC)",
        raw="Casterton",
        jump=datetime(2026, 8, 30, 12, 40, tzinfo=SYD),
        state="VIC",
    )


def _mudgee() -> RaceView:
    return _rv(
        url="https://example.test/mudgee",
        venue="Mudgee (NSW)",
        raw="Mudgee",
        jump=datetime(2026, 8, 30, 13, 25, tzinfo=SYD),
    )


def _app(qapp, tmp_path: Path):
    settings = DesktopSettings(QSettings(str(tmp_path / "s.ini"), QSettings.Format.IniFormat))
    settings.db_path = tmp_path / "roster.db"
    settings.auto_refresh = False
    settings.timezone = "Australia/Sydney"
    controller = ApplicationController(settings)
    controller.chosen_date = D
    window = MainWindow(controller)
    return controller, window


def _assert_hero_upcoming_consistent(window, controller, expected_raw: str | None) -> None:
    state = controller.last_race_day_state or controller.race_day_state()
    shown = window.race_day.hero.view
    if expected_raw is None:
        assert state.hero is None
        assert shown is None
        assert window.race_day.upcoming_model.rowCount() == 0
        return
    assert state.hero is not None
    assert state.hero.venue_raw == expected_raw
    assert shown is not None
    assert shown.race_key == state.hero.race_key
    assert shown.race_key == controller.hero.race_key
    keys = window.race_day.upcoming_model.race_keys()
    assert state.hero.race_key not in keys
    assert shown.race_key not in keys
    assert shown.jump_at is not None and shown.jump_at.tzinfo is not None


def test_chronological_hero_matches_upcoming_table(qapp, tmp_path):
    controller, window = _app(qapp, tmp_path)
    now = datetime(2026, 8, 30, 12, 24, tzinfo=SYD)
    wyong, casterton, mudgee = _wyong(), _casterton(), _mudgee()
    try:
        controller.freeze_now(now)
        controller.views = [mudgee, casterton, wyong]
        controller.sync_race_day(now)
        controller.views_changed.emit()
        qapp.processEvents()
        _assert_hero_upcoming_consistent(window, controller, "Wyong")
        assert "Wyong" in window.race_day.hero.title.text()
        assert "12:35" in window.race_day.hero.meta.text()
        keys = window.race_day.upcoming_model.race_keys()
        assert keys[0] == casterton.race_key
        assert mudgee.race_key in keys
        assert wyong.race_key not in keys
    finally:
        controller.shutdown()
        window.close()


def test_stale_widget_rebinding_on_clock_tick(qapp, tmp_path):
    """Hero widget must follow controller.hero on tick, not keep a private stale RaceView."""
    controller, window = _app(qapp, tmp_path)
    now = datetime(2026, 8, 30, 12, 24, tzinfo=SYD)
    wyong, casterton, mudgee = _wyong(), _casterton(), _mudgee()
    try:
        controller.freeze_now(now)
        controller.views = [mudgee]
        controller.sync_race_day(now)
        controller.views_changed.emit()
        qapp.processEvents()
        assert "Mudgee" in window.race_day.hero.title.text()
        assert "13:25" in window.race_day.hero.meta.text()

        controller.views = [mudgee, casterton, wyong]
        controller._tick()
        qapp.processEvents()

        shown = window.race_day.hero.view
        assert shown is not None
        assert shown.venue_raw == "Wyong"
        assert "Wyong" in window.race_day.hero.title.text()
        assert "12:35" in window.race_day.hero.meta.text()
        assert "11m" in window.race_day.hero.meta.text()
        _assert_hero_upcoming_consistent(window, controller, "Wyong")
        keys = window.race_day.upcoming_model.race_keys()
        assert wyong.race_key not in keys
        assert mudgee.race_key in keys
    finally:
        controller.shutdown()
        window.close()


def test_hero_advances_after_jump_window(qapp, tmp_path):
    controller, window = _app(qapp, tmp_path)
    wyong, casterton, mudgee = _wyong(), _casterton(), _mudgee()
    before = datetime(2026, 8, 30, 12, 34, tzinfo=SYD)
    after = wyong.jump_at + hero_running_hold() + timedelta(seconds=1)
    try:
        controller.freeze_now(before)
        controller.views = [wyong, casterton, mudgee]
        controller.sync_race_day(before)
        controller.views_changed.emit()
        qapp.processEvents()
        _assert_hero_upcoming_consistent(window, controller, "Wyong")

        controller.freeze_now(after)
        controller._tick()
        qapp.processEvents()
        _assert_hero_upcoming_consistent(window, controller, "Casterton")
        assert "Casterton" in window.race_day.hero.title.text()
        assert wyong.race_key not in window.race_day.upcoming_model.race_keys()
        assert mudgee.race_key in window.race_day.upcoming_model.race_keys()
    finally:
        controller.shutdown()
        window.close()


def test_earlier_race_from_later_bundle_replaces_hero(qapp, tmp_path, caplog):
    controller, window = _app(qapp, tmp_path)
    now = datetime(2026, 8, 30, 12, 24, tzinfo=SYD)
    wyong, casterton, mudgee = _wyong(), _casterton(), _mudgee()
    try:
        controller.freeze_now(now)
        controller.views = [mudgee]
        controller.sync_race_day(now)
        controller.views_changed.emit()
        qapp.processEvents()
        assert "Mudgee" in window.race_day.hero.title.text()

        bundle = CardBundle(
            payload=RefreshPayload("card", "success", "ok"),
            views=[mudgee, casterton, wyong],
            kind="card",
            chosen_date=D,
        )
        with caplog.at_level(logging.DEBUG, logger="race_day_rater.controller"):
            controller._on_bundle(bundle)
            qapp.processEvents()
        assert "Wyong" in window.race_day.hero.title.text()
        _assert_hero_upcoming_consistent(window, controller, "Wyong")
        assert "Hero transition" in caplog.text
        assert "Mudgee" in caplog.text
        assert "Wyong" in caplog.text
    finally:
        controller.shutdown()
        window.close()


def test_hero_clears_when_no_upcoming_race(qapp, tmp_path):
    controller, window = _app(qapp, tmp_path)
    finished = _rv(
        url="https://example.test/wyong",
        venue="Wyong (NSW)",
        raw="Wyong",
        jump=datetime(2026, 8, 30, 10, 0, tzinfo=SYD),
        status="finished",
    )
    now = datetime(2026, 8, 30, 18, 0, tzinfo=SYD)
    try:
        controller.freeze_now(datetime(2026, 8, 30, 9, 50, tzinfo=SYD))
        controller.views = [finished]
        finished.status = "upcoming"
        finished.live_status = "upcoming"
        controller.sync_race_day()
        controller.views_changed.emit()
        qapp.processEvents()
        assert window.race_day.hero.view is not None

        finished.status = "finished"
        finished.live_status = "finished"
        controller.freeze_now(now)
        controller._tick()
        qapp.processEvents()
        assert window.race_day.hero.view is None
        assert window.race_day.hero.title.text() == "No upcoming thoroughbred race"
        assert not window.race_day.hero.open_btn.isEnabled()
        assert not window.race_day.hero.lock_btn.isEnabled()
        _assert_hero_upcoming_consistent(window, controller, None)
    finally:
        controller.shutdown()
        window.close()


def test_timezone_aware_ordering_not_clock_strings(qapp, tmp_path):
    controller, window = _app(qapp, tmp_path)
    chosen = date(2026, 10, 4)
    app_tz = SYD
    now = datetime(2026, 10, 4, 11, 0, tzinfo=app_tz)

    def mtg(venue: str, state: str, url: str) -> Meeting:
        return Meeting(
            code="thoroughbred",
            source="racingaustralia",
            venue=venue,
            meeting_date=chosen,
            first_race_time_local=None,
            num_races=1,
            meeting_url=url,
            status="upcoming",
            extra={"state": state, "key": f"2026Oct04,{state},{venue}"},
        )

    gawler_m = mtg("Gawler", "SA", "https://example.test/gawler")
    wyong_m = mtg("Wyong", "NSW", "https://example.test/wyong")
    casterton_m = mtg("Casterton", "VIC", "https://example.test/casterton")
    toowoomba_m = mtg("Toowoomba", "QLD", "https://example.test/toowoomba")
    gawler_j = jump_datetime(chosen_date=chosen, start_time_local=time(12, 0), meeting=gawler_m, app_tz=app_tz)
    wyong_j = jump_datetime(chosen_date=chosen, start_time_local=time(12, 35), meeting=wyong_m, app_tz=app_tz)
    casterton_j = jump_datetime(chosen_date=chosen, start_time_local=time(13, 0), meeting=casterton_m, app_tz=app_tz)
    toowoomba_j = jump_datetime(chosen_date=chosen, start_time_local=time(12, 40), meeting=toowoomba_m, app_tz=app_tz)
    assert all(j is not None and j.tzinfo is not None for j in (gawler_j, wyong_j, casterton_j, toowoomba_j))
    assert gawler_j < wyong_j < casterton_j < toowoomba_j

    views = [
        _rv(url=toowoomba_m.meeting_url, venue="Toowoomba (QLD)", raw="Toowoomba", jump=toowoomba_j, state="QLD"),
        _rv(url=casterton_m.meeting_url, venue="Casterton (VIC)", raw="Casterton", jump=casterton_j, state="VIC"),
        _rv(url=wyong_m.meeting_url, venue="Wyong (NSW)", raw="Wyong", jump=wyong_j, state="NSW"),
        _rv(url=gawler_m.meeting_url, venue="Gawler (SA)", raw="Gawler", jump=gawler_j, state="SA"),
    ]
    try:
        controller.chosen_date = chosen
        controller.freeze_now(now)
        controller.views = views
        controller.sync_race_day(now)
        controller.views_changed.emit()
        qapp.processEvents()
        _assert_hero_upcoming_consistent(window, controller, "Gawler")
        raws = [window.race_day.upcoming_model.row_at(i)["venue"] for i in range(window.race_day.upcoming_model.rowCount())]
        assert raws[0].startswith("Wyong")
        # Same local clock 12:40 QLD is later than 13:00 VIC after conversion.
        assert any(v.startswith("Toowoomba") for v in raws)
        assert raws.index(next(v for v in raws if v.startswith("Casterton"))) < raws.index(
            next(v for v in raws if v.startswith("Toowoomba"))
        )
    finally:
        controller.shutdown()
        window.close()
