from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt

from desktop.models.picks_table_model import PicksTableModel
from desktop.models.race_table_model import HEADERS, RACE_KEY_ROLE, RaceTableModel, race_to_row
from models import Meeting, Race, Runner
from services.formatting import format_runner_pick
from services.race_day_service import RaceView, build_race_views, upcoming_races
from services.result_service import BACKUP_WON, WIN

SYD = ZoneInfo("Australia/Sydney")


def _runner(name, draw, program_number, finishes=None):
    return Runner(
        code="thoroughbred",
        name=name,
        draw=draw,
        recent_finishes=finishes or [1, 2, 3],
        early_speed=None,
        program_number=program_number,
        weight_kg=56.0,
        benchmark=64.0,
    )


def _view(**kw) -> RaceView:
    base = dict(
        meeting_url="https://example.test/m",
        race_url="https://example.test/r",
        code="thoroughbred",
        venue="Randwick (NSW)",
        venue_raw="Randwick",
        state="NSW",
        race_no=2,
        race_name="BM64",
        race_class="BM64",
        distance_m=1400,
        track_condition="Good4",
        jump_at=datetime(2026, 8, 29, 13, 20, tzinfo=SYD),
        status="upcoming",
        primary="Sarah's Sonnets",
        primary_no="5",
        backup="Dracena",
        backup_no="4",
        primary_score=0.7,
        backup_score=0.6,
        score_gap=0.1,
        confidence_label="Strong",
        odds=4.8,
        backup_odds=11.0,
        field_size=8,
        scratching_warning=False,
        locked=False,
        from_snapshot=False,
        live_status="upcoming",
        runners=[_runner("Sarah's Sonnets", 12, 5), _runner("Dracena", 1, 4)],
    )
    base.update(kw)
    return RaceView(**base)


def test_race_table_columns_and_program_number_not_barrier(qapp):
    now = datetime(2026, 8, 29, 13, 0, tzinfo=SYD)
    row = race_to_row(_view(), now)
    assert row["primary"].startswith("5.")
    assert "SARAH" in row["primary"]
    assert row["backup"].startswith("4.")
    assert "12" not in row["primary"].split()[0]
    model = RaceTableModel()
    model.set_rows([row])
    assert model.columnCount() == len(HEADERS)
    assert model.rowCount() == 1
    assert model.data(model.index(0, 4), Qt.ItemDataRole.DisplayRole).startswith("5.")
    assert model.data(model.index(0, 0), RACE_KEY_ROLE) == ("https://example.test/m", 2)


def test_hero_and_upcoming_formatting(qapp):
    text = format_runner_pick(5, "Sarah’s Sonnets", 4.8)
    assert text == "5. SARAH’S SONNETS · $4.80"
    backup = format_runner_pick(4, "Dracena", 11)
    assert backup == "4. DRACENA · $11.00"
    now = datetime(2026, 8, 29, 13, 0, tzinfo=SYD)
    row = race_to_row(_view(), now)
    assert "5." in row["primary"]
    assert row["odds"] == "$4.80"


def test_chronological_upcoming_order(qapp):
    meeting = Meeting(
        code="thoroughbred",
        source="racingaustralia",
        venue="Randwick",
        meeting_date=date(2026, 8, 29),
        first_race_time_local=time(12, 0),
        num_races=2,
        meeting_url="https://example.test/randwick",
        status="upcoming",
        extra={"state": "NSW", "key": "2026Aug29,NSW,Randwick"},
    )
    fields = {
        meeting.meeting_url: {
            "races": [
                Race("thoroughbred", 1, "R1", 1200, time(13, 40), "https://example.test/r1", {}),
                Race("thoroughbred", 2, "R2", 1400, time(13, 20), "https://example.test/r2", {}),
            ],
            "runners_by_race": {
                1: [_runner("Later", 3, 3, [4, 5, 6])],
                2: [_runner("Soon", 8, 2, [1, 1, 2]), _runner("SoonTwo", 1, 1, [6, 7, 8])],
            },
            "meta": {"track_condition": "Good 4"},
        }
    }
    now = datetime(2026, 8, 29, 13, 0, tzinfo=SYD)
    views = build_race_views(
        chosen_date=date(2026, 8, 29),
        meetings=[meeting],
        fields_by_meeting=fields,
        now=now,
        app_tz=SYD,
    )
    up = upcoming_races(views, now, limit=8)
    times = [v.jump_at for v in up]
    assert times == sorted(times)
    rows = [race_to_row(v, now) for v in up]
    model = RaceTableModel()
    model.set_rows(rows)
    first = model.row_at(0)
    assert first is not None


def test_result_status_colours(qapp):
    from desktop.roles import ROW_TONE_ROLE
    from desktop.themes.theme_manager import current

    model = PicksTableModel()
    model.set_rows(
        [
            {
                "result": WIN,
                "jump": "13:00",
                "venue": "Randwick",
                "race": "R1",
                "primary": "5. ALPHA",
                "primary_finish": "1st",
                "saved_odds": 4.0,
                "backup": "4. BETA",
                "backup_finish": "—",
                "confidence": "Strong",
                "source": "ok",
                "row_tone": "win",
            },
            {
                "result": BACKUP_WON,
                "jump": "13:20",
                "venue": "Randwick",
                "race": "R2",
                "primary": "1. GAMMA",
                "primary_finish": "SCR",
                "saved_odds": 6.0,
                "backup": "2. DELTA",
                "backup_finish": "1st",
                "confidence": "Close race",
                "source": "ok",
                "row_tone": "backup_won",
            },
        ]
    )
    assert model.data(model.index(0, 0), ROW_TONE_ROLE) == "win"
    assert model.data(model.index(1, 0), ROW_TONE_ROLE) == "backup_won"
    theme = current()
    assert theme.semantic.win.name() != theme.semantic.backup_win.name()
    assert theme.semantic.win.name() != theme.table.selection.name()


def test_selection_preserved_across_set_rows(qapp):
    now = datetime(2026, 8, 29, 13, 0, tzinfo=SYD)
    a = race_to_row(_view(race_no=1, meeting_url="https://example.test/a"), now)
    b = race_to_row(_view(race_no=2, meeting_url="https://example.test/b"), now)
    model = RaceTableModel()
    model.set_rows([a, b])
    key = b["race_key"]
    model.set_rows([b, a])
    assert model.find_row(key) == 0
