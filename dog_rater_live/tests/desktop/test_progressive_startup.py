from __future__ import annotations

import os
import threading
import time
from datetime import date, datetime, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings

from desktop.application_controller import ApplicationController
from desktop.main_window import MainWindow
from desktop.settings import DesktopSettings
from desktop.status import EMPTY_MESSAGE, LOADING_MESSAGE
from desktop.workers.refresh_worker import CardBundle
from models import Meeting, Race, Runner
from race_db import persist_daily_fields, persist_daily_meetings
from services.card_loader import MEETINGS_CODE, RefreshPayload
from services.race_day_service import build_race_views, resolve_tz

D = date(2026, 8, 29)
NOW = datetime(2026, 8, 29, 12, 0, tzinfo=ZoneInfo("Australia/Sydney"))


def _runner(name: str, draw: int, no: int) -> Runner:
    return Runner(
        code="thoroughbred",
        name=name,
        draw=draw,
        recent_finishes=[1, 2, 3],
        early_speed=None,
        program_number=no,
        weight_kg=56.0,
        benchmark=64.0,
    )


def _meeting(url: str, venue: str, state: str = "NSW") -> Meeting:
    return Meeting(
        code="thoroughbred",
        source="racingaustralia",
        venue=venue,
        meeting_date=D,
        first_race_time_local=dtime(23, 0),
        num_races=3,
        meeting_url=url,
        status="upcoming",
        extra={"state": state, "key": f"2026Aug29,{state},{venue}"},
    )


def _fields(url: str) -> dict:
    races = [
        Race("thoroughbred", 1, "BM64", 1200, dtime(23, 10), url + "/r1", {}),
        Race("thoroughbred", 2, "BM70", 1400, dtime(23, 30), url + "/r2", {}),
        Race("thoroughbred", 3, "BM78", 1600, dtime(23, 50), url + "/r3", {}),
    ]
    runners = {
        1: [_runner("Alpha One", 8, 5), _runner("Beta Two", 1, 4)],
        2: [_runner("Gamma Three", 3, 2), _runner("Delta Four", 7, 1)],
        3: [_runner("Epsilon Five", 4, 8), _runner("Zeta Six", 2, 3)],
    }
    return {url: {"races": races, "runners_by_race": runners, "meta": {"track_condition": "Good 4"}}}


def _seed(db: Path, url: str = "https://example.test/randwick", venue: str = "Randwick") -> tuple[list, dict]:
    meetings = [_meeting(url, venue)]
    fields = _fields(url)
    persist_daily_meetings(D, MEETINGS_CODE, meetings, db_path=db)
    persist_daily_fields(
        D,
        url,
        (fields[url]["races"], fields[url]["runners_by_race"], fields[url]["meta"]),
        db_path=db,
    )
    return meetings, fields


def _pump(qapp, pred, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        qapp.processEvents()
        if pred():
            return True
        time.sleep(0.02)
    return False


def _app(qapp, tmp_path: Path):
    ini = str(tmp_path / "s.ini")
    settings = DesktopSettings(QSettings(ini, QSettings.Format.IniFormat))
    settings.db_path = tmp_path / "roster.db"
    settings.auto_refresh = False
    controller = ApplicationController(settings)
    controller.chosen_date = D
    controller.freeze_now(NOW)
    window = MainWindow(controller)
    return controller, window


@pytest.fixture
def no_live(monkeypatch):
    monkeypatch.setattr(
        "services.card_loader.fetch_tb_meetings",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no live meetings in tests")),
    )
    monkeypatch.setattr(
        "services.card_loader.fetch_tb_fields",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no live fields in tests")),
    )
    monkeypatch.setattr(
        "desktop.workers.refresh_worker.fetch_odds_bundle",
        lambda *a, **k: ({}, {}, []),
    )
    monkeypatch.setattr(
        "desktop.workers.refresh_worker.sync_missing_results",
        lambda **k: None,
    )


def test_controller_path_populates_tables_without_odds(qapp, tmp_path, no_live):
    meetings, fields = _seed(tmp_path / "roster.db")
    controller, window = _app(qapp, tmp_path)
    seen = []
    controller.views_changed.connect(lambda: seen.append(len(controller.views)))
    try:
        controller.start()
        assert _pump(qapp, lambda: bool(controller.views))
        assert seen
        assert len(controller.views) >= 2
        assert window.race_day.upcoming_model.rowCount() >= 1
        assert controller.hero is not None
        assert controller.hero.venue_raw == "Randwick"
        assert all(v.odds is None for v in controller.views)
    finally:
        controller.shutdown()
        window.close()


def test_cached_views_appear_before_delayed_live(qapp, tmp_path, monkeypatch):
    _seed(tmp_path / "roster.db")
    release = threading.Event()

    def slow_meetings(*_a, **_k):
        release.wait(timeout=8)
        return [_meeting("https://example.test/live", "Eagle Farm", "QLD")]

    monkeypatch.setattr("services.card_loader.fetch_tb_meetings", slow_meetings)
    monkeypatch.setattr(
        "services.card_loader.fetch_tb_fields",
        lambda *a, **k: (
            _fields("https://example.test/live")["https://example.test/live"]["races"],
            _fields("https://example.test/live")["https://example.test/live"]["runners_by_race"],
            {},
        ),
    )
    monkeypatch.setattr("desktop.workers.refresh_worker.fetch_odds_bundle", lambda *a, **k: ({}, {}, []))
    monkeypatch.setattr("desktop.workers.refresh_worker.sync_missing_results", lambda **k: None)

    controller, window = _app(qapp, tmp_path)
    try:
        controller.start()
        assert _pump(qapp, lambda: bool(controller.views), timeout=3)
        assert not release.is_set()
        assert any(v.venue_raw == "Randwick" for v in controller.views)
        release.set()
        _pump(qapp, lambda: any(v.venue_raw == "Eagle Farm" for v in controller.views), timeout=4)
    finally:
        release.set()
        controller.shutdown()
        window.close()


def test_card_emits_before_odds_and_results(qapp, tmp_path, monkeypatch):
    meetings, fields = _seed(tmp_path / "roster.db")
    order: list[str] = []
    odds_block = threading.Event()
    results_block = threading.Event()

    def live_meetings(*_a, **_k):
        order.append("meetings")
        return meetings

    def live_fields(*_a, **_k):
        order.append("fields")
        url = meetings[0].meeting_url
        mf = fields[url]
        return mf["races"], mf["runners_by_race"], mf["meta"]

    def odds(*_a, **_k):
        order.append("odds")
        odds_block.wait(timeout=8)
        return {}, {}, ["Odds feed unavailable."]

    def results(**_k):
        order.append("results")
        results_block.wait(timeout=8)

    monkeypatch.setattr("services.card_loader.fetch_tb_meetings", live_meetings)
    monkeypatch.setattr("services.card_loader.fetch_tb_fields", live_fields)
    monkeypatch.setattr("desktop.workers.refresh_worker.fetch_odds_bundle", odds)
    monkeypatch.setattr("desktop.workers.refresh_worker.sync_missing_results", results)

    controller, window = _app(qapp, tmp_path)
    try:
        controller.start()
        assert _pump(qapp, lambda: bool(controller.views), timeout=4)
        assert "odds" not in order or order.index("fields") < order.index("odds")
        assert "results" not in order or ("odds" in order and order.index("fields") < order.index("results"))
        assert window.race_day.upcoming_model.rowCount() >= 1
        odds_block.set()
        results_block.set()
        _pump(qapp, lambda: "odds" in order and "results" in order, timeout=4)
        assert order.index("fields") < order.index("odds")
        assert order.index("fields") < order.index("results")
    finally:
        odds_block.set()
        results_block.set()
        controller.shutdown()
        window.close()


def test_odds_and_results_failure_leave_races(qapp, tmp_path, monkeypatch):
    meetings, fields = _seed(tmp_path / "roster.db")

    monkeypatch.setattr("services.card_loader.fetch_tb_meetings", lambda *a, **k: meetings)
    monkeypatch.setattr(
        "services.card_loader.fetch_tb_fields",
        lambda *a, **k: (
            fields[meetings[0].meeting_url]["races"],
            fields[meetings[0].meeting_url]["runners_by_race"],
            fields[meetings[0].meeting_url]["meta"],
        ),
    )
    monkeypatch.setattr(
        "desktop.workers.refresh_worker.fetch_odds_bundle",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("odds down")),
    )
    monkeypatch.setattr(
        "desktop.workers.refresh_worker.sync_missing_results",
        lambda **k: (_ for _ in ()).throw(RuntimeError("results down")),
    )

    controller, window = _app(qapp, tmp_path)
    try:
        controller.start()
        assert _pump(qapp, lambda: bool(controller.views))
        _pump(qapp, lambda: False, timeout=0.8)
        assert controller.views
        assert window.race_day.upcoming_model.rowCount() >= 1
        assert controller.hero is not None
    finally:
        controller.shutdown()
        window.close()


def test_empty_incoming_does_not_wipe_card(qapp, tmp_path, no_live):
    meetings, fields = _seed(tmp_path / "roster.db")
    controller, window = _app(qapp, tmp_path)
    try:
        tz = resolve_tz("Australia/Sydney")
        now = NOW
        controller.meetings = meetings
        controller.fields = fields
        controller.views = build_race_views(
            chosen_date=D,
            meetings=meetings,
            fields_by_meeting=fields,
            now=now,
            app_tz=tz,
        )
        n = len(controller.views)
        assert n >= 2
        payload = RefreshPayload(
            kind="card",
            status="success",
            message="empty",
            meetings=[],
            fields_by_meeting={},
            chosen_date=D,
        )
        controller._on_bundle(CardBundle(payload=payload, views=[], kind="card", chosen_date=D))
        assert len(controller.views) == n
        assert controller.meetings
    finally:
        controller.shutdown()
        window.close()


def test_loading_message_not_replaced_by_empty_state(qapp, tmp_path, monkeypatch):
    block = threading.Event()

    monkeypatch.setattr(
        "services.card_loader.fetch_tb_meetings",
        lambda *a, **k: block.wait(timeout=8) or [],
    )
    monkeypatch.setattr("services.card_loader.fetch_tb_fields", lambda *a, **k: ([], {}, {}))
    monkeypatch.setattr("desktop.workers.refresh_worker.fetch_odds_bundle", lambda *a, **k: ({}, {}, []))
    monkeypatch.setattr("desktop.workers.refresh_worker.sync_missing_results", lambda **k: None)

    controller, window = _app(qapp, tmp_path)
    try:
        controller.start()
        assert _pump(qapp, lambda: LOADING_MESSAGE in (window.race_day.empty.text() or ""), timeout=2)
        assert EMPTY_MESSAGE not in (window.race_day.empty.text() or "")
        assert not controller.views
    finally:
        block.set()
        controller.shutdown()
        window.close()


def test_refresh_gate_keeps_results_follow_up():
    from desktop.refresh_gate import RefreshGate

    g = RefreshGate()
    assert g.request("card") is True
    assert g.request("odds") is False
    assert g.request("results") is False
    follow = g.finish()
    assert follow == "odds"
    assert g.request(follow) is True
    follow2 = g.finish()
    assert follow2 == "results"


def test_manual_refresh_coalesces(qapp, tmp_path, monkeypatch):
    meetings, fields = _seed(tmp_path / "roster.db")
    calls = []
    block = threading.Event()

    def live_meetings(*_a, **_k):
        calls.append("meetings")
        block.wait(timeout=8)
        return meetings

    monkeypatch.setattr("services.card_loader.fetch_tb_meetings", live_meetings)
    monkeypatch.setattr(
        "services.card_loader.fetch_tb_fields",
        lambda *a, **k: (
            fields[meetings[0].meeting_url]["races"],
            fields[meetings[0].meeting_url]["runners_by_race"],
            {},
        ),
    )
    monkeypatch.setattr("desktop.workers.refresh_worker.fetch_odds_bundle", lambda *a, **k: ({}, {}, []))
    monkeypatch.setattr("desktop.workers.refresh_worker.sync_missing_results", lambda **k: None)

    controller, window = _app(qapp, tmp_path)
    try:
        controller.start()
        _pump(qapp, lambda: bool(controller.views), timeout=3)
        window.refresh_btn.click()
        window.refresh_btn.click()
        qapp.processEvents()
        assert not window.refresh_btn.isEnabled() or controller._card_busy
        block.set()
        _pump(qapp, lambda: window.refresh_btn.isEnabled(), timeout=4)
    finally:
        block.set()
        controller.shutdown()
        window.close()
