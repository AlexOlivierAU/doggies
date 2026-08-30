from __future__ import annotations

import os
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings

from desktop.application_controller import ApplicationController
from desktop.main_window import MainWindow
from desktop.models.details_table_model import details_rows
from desktop.models.race_table_model import race_to_row
from desktop.settings import DesktopSettings
from models import Meeting, Race, Runner
from odds_sportsbet import norm_horse_name
from services.scratching import reset_logged_transitions

SYD = ZoneInfo("Australia/Sydney")
D = date(2026, 8, 29)
URL = "https://example.test/casterton"
NOW = datetime(2026, 8, 29, 12, 0, tzinfo=SYD)


def _runner(name, draw, no, finishes):
    return Runner(
        code="thoroughbred",
        name=name,
        draw=draw,
        recent_finishes=finishes,
        early_speed=None,
        weight_kg=56.0,
        benchmark=64.0,
        program_number=no,
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


def test_desktop_hero_promotes_sportsbet_is_out(qapp, tmp_path):
    reset_logged_transitions()
    controller, window = _app(qapp, tmp_path)
    meeting = Meeting(
        code="thoroughbred",
        source="racingaustralia",
        venue="Casterton",
        meeting_date=D,
        first_race_time_local=time(12, 35),
        num_races=2,
        meeting_url=URL,
        status="upcoming",
        extra={"state": "VIC", "key": "2026Aug29,VIC,Casterton"},
    )
    race = Race("thoroughbred", 2, "R2", 1200, time(12, 35), URL + "/r2", {})
    try:
        controller.freeze_now(NOW)
        controller.meetings = [meeting]
        controller.fields = {
            URL: {
                "races": [race],
                "runners_by_race": {
                    2: [
                        _runner("QUYNH", 1, 12, [1, 1, 1, 1]),
                        _runner("PIKLEMEGRANDMOTHER", 2, 10, [2, 2, 3, 4]),
                        _runner("THIRD HORSE", 3, 8, [8, 8, 8, 8]),
                    ]
                },
                "meta": {"track_condition": "Good 4"},
            }
        }
        controller.odds_index = {("casterton", 2): 42}
        controller.odds_by_event = {
            42: {
                norm_horse_name("QUYNH"): {
                    "name": "QUYNH",
                    "no": 12,
                    "scratched": True,
                    "isOut": True,
                    "source": "sportsbet",
                },
                norm_horse_name("PIKLEMEGRANDMOTHER"): {
                    "name": "PIKLEMEGRANDMOTHER",
                    "no": 10,
                    "win": 6.5,
                    "scratched": False,
                    "source": "sportsbet",
                },
                norm_horse_name("THIRD HORSE"): {
                    "name": "THIRD HORSE",
                    "no": 8,
                    "win": 9.0,
                    "scratched": False,
                    "source": "sportsbet",
                },
            }
        }
        controller._rebuild_views_local()
        controller.views_changed.emit()
        qapp.processEvents()

        assert controller.views
        view = controller.views[0]
        assert view.primary == "PIKLEMEGRANDMOTHER"
        assert view.primary != "QUYNH"
        assert next(r for r in view.runners if r.name == "QUYNH").scratched is True
        assert view.field_size == 2

        shown = window.race_day.hero.primary.text()
        assert "PIKLEMEGRANDMOTHER" in shown
        assert "QUYNH" not in shown or "SCRATCHED" in shown
        assert window.race_day.hero.lock_btn.isEnabled()
        assert controller.lock_view(view) is True

        rows = details_rows(view, view.ranked)
        quynh = next(r for r in rows if r["raw_name"] == "QUYNH")
        assert quynh["scratched"] is True
        assert "SCR" in str(quynh.get("role") or quynh.get("status") or "")
        assert not any(r["raw_name"] == "QUYNH" and r.get("role") in {"PRIMARY", "PROMOTED"} for r in rows)

        upcoming = race_to_row(view, NOW)
        assert "QUYNH" not in upcoming["primary"]
        assert "PIKLEMEGRANDMOTHER" in upcoming["primary"]
    finally:
        from desktop.images.silk_cache import reset_silk_cache

        reset_silk_cache()
        controller.shutdown()
        window.close()


def test_desktop_lock_disabled_without_active_selection(qapp, tmp_path):
    reset_logged_transitions()
    controller, window = _app(qapp, tmp_path)
    meeting = Meeting(
        code="thoroughbred",
        source="racingaustralia",
        venue="Casterton",
        meeting_date=D,
        first_race_time_local=time(12, 35),
        num_races=2,
        meeting_url=URL,
        status="upcoming",
        extra={"state": "VIC"},
    )
    race = Race("thoroughbred", 2, "R2", 1200, time(12, 35), URL + "/r2", {})
    try:
        controller.freeze_now(NOW)
        controller.meetings = [meeting]
        controller.fields = {
            URL: {
                "races": [race],
                "runners_by_race": {
                    2: [
                        _runner("QUYNH", 1, 12, [1, 1, 1, 1]),
                        _runner("PIKLEMEGRANDMOTHER", 2, 10, [2, 2, 3, 4]),
                    ]
                },
                "meta": {},
            }
        }
        controller.odds_index = {("casterton", 2): 7}
        controller.odds_by_event = {
            7: {
                norm_horse_name("QUYNH"): {"name": "QUYNH", "no": 12, "isOut": True, "source": "sportsbet"},
                norm_horse_name("PIKLEMEGRANDMOTHER"): {
                    "name": "PIKLEMEGRANDMOTHER",
                    "no": 10,
                    "isOut": True,
                    "source": "sportsbet",
                },
            }
        }
        controller._rebuild_views_local()
        controller.views_changed.emit()
        qapp.processEvents()
        view = controller.views[0]
        assert view.no_active_selection is True
        assert "NO ACTIVE SELECTION" in window.race_day.hero.primary.text()
        assert window.race_day.hero.lock_btn.isEnabled() is False
        assert controller.lock_view(view) is False
    finally:
        from desktop.images.silk_cache import reset_silk_cache

        reset_silk_cache()
        controller.shutdown()
        window.close()
