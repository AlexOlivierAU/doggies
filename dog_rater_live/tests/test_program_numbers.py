from __future__ import annotations

from datetime import date, datetime, time
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from models import Meeting, Race, Runner
from parse_racingaustralia import official_program_number
from race_db import get_pick, save_pick
from services.formatting import format_runner_pick, format_saved_selection, markdown_safe_pick
from services.pick_service import build_snapshot_payload, save_selection_snapshot, snapshot_field
from services.race_day_service import build_race_views, runner_program_number
from services.runner_numbers import (
    parse_program_number_cell,
    program_number_for_runner,
    program_number_from_raw,
    saved_pick_number,
)
from ui.components import pick_cell, pick_metric_html


SYD = ZoneInfo("Australia/Sydney")
RA_HEADERS = [
    "No",
    "Last 10",
    "Horse",
    "Sex",
    "Age",
    "Trainer",
    "Jockey",
    "Barrier",
    "Weight",
    "Hcp Rating",
]


def _runner(name: str, draw: int, program_number: int | None = None, scratched: bool = False, raw=None) -> Runner:
    return Runner(
        code="thoroughbred",
        name=name,
        draw=draw,
        recent_finishes=[1, 2, 3],
        early_speed=None,
        scratched=scratched,
        weight_kg=56.0,
        benchmark=64.0,
        raw=raw or {},
        program_number=program_number,
    )


def _ra_cells(no: str, horse: str, barrier: str, *, last10: str = "x12", status: str | None = None) -> tuple[list[str], list[str]]:
    headers = list(RA_HEADERS)
    cells = [no, last10, horse, "M", "4", "Trainer", "Jockey", barrier, "58", "64"]
    if status is not None:
        headers.append("Status")
        cells.append(status)
    return headers, cells


def test_explicit_program_number_preferred_over_raw_and_barrier():
    headers, cells = _ra_cells("7", "Sarah's Sonnets", "12")
    runner = _runner(
        "Sarah's Sonnets",
        draw=12,
        program_number=5,
        raw={"headers": headers, "cells": cells, "program_number": 7},
    )
    assert program_number_for_runner(runner) == 5
    assert runner.draw == 12
    assert program_number_for_runner(runner) != runner.draw


def test_runner_number_is_distinct_from_barrier():
    headers, cells = _ra_cells("5", "Sarah's Sonnets", "12")
    runner = _runner(
        "Sarah's Sonnets",
        draw=12,
        program_number=5,
        raw={"headers": headers, "cells": cells},
    )
    assert runner.program_number == 5
    assert runner.draw == 12
    assert runner_program_number(runner) == "5"


def test_number_parsed_from_realistic_racing_australia_cells():
    cases = [
        ("5", "12", 5),
        ("5.", "12", 5),
        ("5 (12)", "8", 5),
        ("5e", "12", 5),
        ("5E", "3", 5),
    ]
    for no_cell, barrier, expected in cases:
        headers, cells = _ra_cells(no_cell, "Sarah's Sonnets", barrier)
        assert official_program_number(headers, cells) == expected
        assert parse_program_number_cell(no_cell) == expected
        assert program_number_from_raw(headers, cells) == expected


def test_emergency_and_scratched_runner_numbers():
    headers, cells = _ra_cells("5e", "Emergency Colt", "14")
    assert official_program_number(headers, cells) == 5

    headers, cells = _ra_cells("4", "Scratched Mare", "2", status="SCR")
    assert official_program_number(headers, cells) == 4

    html = """
    <table>
      <tr><th>No</th><th>Horse</th><th>Barrier</th><th>Weight</th><th>Status</th></tr>
      <tr><td>5e</td><td>Emergency Colt</td><td>14</td><td>58</td><td></td></tr>
      <tr><td>4</td><td>Scratched Mare</td><td>2</td><td>57</td><td>SCR</td></tr>
    </table>
    """
    table = BeautifulSoup(html, "html.parser").find("table")
    headers = [th.get_text(" ", strip=True) for th in table.find_all("th")]
    numbers = []
    for tr in table.find_all("tr"):
        tds = tr.find_all("td")
        if not tds:
            continue
        cells = [td.get_text(" ", strip=True) for td in tds]
        numbers.append(official_program_number(headers, cells))
    assert numbers == [5, 4]


def test_missing_number_is_not_invented_from_barrier():
    runner = _runner("Sarah's Sonnets", draw=12, program_number=None, raw={})
    assert program_number_for_runner(runner) is None
    assert runner_program_number(runner) == ""
    assert format_runner_pick(number=None, name="Sarah's Sonnets") == "SARAH'S SONNETS"
    assert format_runner_pick(number=0, name="Sarah's Sonnets") == "SARAH'S SONNETS"
    assert format_runner_pick(number="None", name="Sarah's Sonnets") == "SARAH'S SONNETS"
    assert not format_runner_pick(number=None, name="Sarah's Sonnets").startswith(".")
    headers = ["Horse", "Barrier", "Weight"]
    cells = ["Sarah's Sonnets", "12", "58"]
    assert official_program_number(headers, cells) is None


def test_hero_primary_and_backup_formatting():
    primary = format_runner_pick(number=5, name="Sarah’s Sonnets", odds=4.8)
    backup = format_runner_pick(number=4, name="Dracena", odds=11)
    assert primary == "5. SARAH’S SONNETS · $4.80"
    assert backup == "4. DRACENA · $11.00"
    assert pick_cell(5, "Sarah’s Sonnets", 4.8) == primary
    assert pick_cell(4, "Dracena", 11) == backup
    html = pick_metric_html("Primary", primary)
    assert "5. SARAH’S SONNETS" in html
    assert "<ol>" not in html
    assert markdown_safe_pick(primary).startswith("5\\. ")


def test_upcoming_table_formatting():
    assert pick_cell(5, "Sarah’s Sonnets", 4.8) == "5. SARAH’S SONNETS · $4.80"
    assert pick_cell(4, "Dracena", None) == "4. DRACENA"
    assert pick_cell("", "Only A Name") == "ONLY A NAME"


def test_snapshot_saves_primary_and_backup_numbers(tmp_path: Path):
    db = tmp_path / "roster.db"
    d = date(2026, 8, 29)
    runners = [
        _runner("Sarah's Sonnets", draw=12, program_number=5),
        _runner("Dracena", draw=1, program_number=4),
    ]
    payload = build_snapshot_payload(
        meeting_date=d,
        code="thoroughbred",
        venue="Randwick",
        meeting_url="https://example.test/randwick",
        race_no=3,
        race_name="BM64",
        race_url="https://example.test/r3",
        primary="Sarah's Sonnets",
        backup="Dracena",
        primary_score=0.71,
        backup_score=0.62,
        primary_odds=4.8,
        backup_odds=11.0,
        field=snapshot_field(runners),
        primary_number=5,
        backup_number=4,
    )
    assert payload["primary_number"] == 5
    assert payload["backup_number"] == 4
    assert payload["snapshot"]["primary_number"] == 5
    assert payload["snapshot"]["backup_number"] == 4
    assert payload["snapshot"]["field"][0]["program_number"] == 5
    assert payload["snapshot"]["field"][0]["draw"] == 12

    ok = save_selection_snapshot(
        meeting_date=d,
        meeting_url="https://example.test/randwick",
        code="thoroughbred",
        race_no=3,
        venue="Randwick",
        race_label="R3",
        primary="Sarah's Sonnets",
        backup="Dracena",
        pick_data=payload,
        best_score=0.71,
        backup_score=0.62,
        lock=True,
        db_path=db,
    )
    assert ok
    stored = get_pick(d, "https://example.test/randwick", 3, db_path=db)
    assert stored["primary_number"] == 5
    assert stored["backup_number"] == 4
    assert stored["snapshot"]["primary_number"] == 5
    assert stored["snapshot"]["field"][1]["program_number"] == 4


def test_locked_snapshots_preserve_numbers_not_live_card(tmp_path: Path):
    db = tmp_path / "roster.db"
    d = date(2026, 8, 29)
    payload = build_snapshot_payload(
        meeting_date=d,
        code="thoroughbred",
        venue="Randwick",
        meeting_url="https://example.test/randwick",
        race_no=1,
        race_name="BM64",
        race_url="https://example.test/r1",
        primary="Sarah's Sonnets",
        backup="Dracena",
        primary_score=0.7,
        backup_score=0.6,
        field=snapshot_field(
            [
                _runner("Sarah's Sonnets", draw=12, program_number=5),
                _runner("Dracena", draw=1, program_number=4),
            ]
        ),
        primary_number=5,
        backup_number=4,
    )
    save_selection_snapshot(
        meeting_date=d,
        meeting_url="https://example.test/randwick",
        code="thoroughbred",
        race_no=1,
        venue="Randwick",
        race_label="R1",
        primary="Sarah's Sonnets",
        backup="Dracena",
        pick_data=payload,
        lock=True,
        db_path=db,
    )
    stored = get_pick(d, "https://example.test/randwick", 1, db_path=db)
    live = [
        _runner("Sarah's Sonnets", draw=12, program_number=9),
        _runner("Dracena", draw=1, program_number=8),
    ]
    meeting = Meeting(
        code="thoroughbred",
        source="racingaustralia",
        venue="Randwick",
        meeting_date=d,
        first_race_time_local=time(12, 0),
        num_races=1,
        meeting_url="https://example.test/randwick",
        status="upcoming",
        extra={"state": "NSW", "key": "2026Aug29,NSW,Randwick"},
    )
    views = build_race_views(
        chosen_date=d,
        meetings=[meeting],
        fields_by_meeting={
            meeting.meeting_url: {
                "races": [
                    Race(
                        code="thoroughbred",
                        race_no=1,
                        name="BM64",
                        distance_m=1200,
                        start_time_local=time(13, 0),
                        race_url="https://example.test/r1",
                        extra={},
                    )
                ],
                "runners_by_race": {1: live},
                "meta": {"track_condition": "Good 4"},
            }
        },
        now=datetime(2026, 8, 29, 12, 0, tzinfo=SYD),
        app_tz=SYD,
        saved_picks={(meeting.meeting_url, 1): stored},
    )
    assert views[0].from_snapshot is True
    assert views[0].primary_no == "5"
    assert views[0].backup_no == "4"
    assert saved_pick_number(stored, "primary") == 5
    assert format_saved_selection(stored, "primary").startswith("5. SARAH")
    assert format_saved_selection(stored, "backup").startswith("4. DRACENA")


def test_legacy_snapshots_without_numbers_still_load(tmp_path: Path):
    db = tmp_path / "roster.db"
    d = date(2026, 8, 1)
    save_pick(
        d,
        "https://example.test/old",
        "thoroughbred",
        1,
        "Flemington",
        "R1",
        "Legacy Horse",
        backup="Legacy Backup",
        pick_data={
            "pick_name": "Legacy Horse",
            "backup": "Legacy Backup",
            "snapshot": {"field": [{"name": "Legacy Horse", "draw": 8, "scratched": False}]},
        },
        db_path=db,
    )
    stored = get_pick(d, "https://example.test/old", 1, db_path=db)
    assert stored is not None
    assert stored.get("primary_number") is None
    assert stored.get("backup_number") is None
    assert saved_pick_number(stored, "primary") is None
    assert format_saved_selection(stored, "primary") == "LEGACY HORSE"
    assert format_saved_selection(stored, "backup") == "LEGACY BACKUP"


def test_legacy_field_snapshot_can_supply_number(tmp_path: Path):
    db = tmp_path / "roster.db"
    d = date(2026, 8, 1)
    save_pick(
        d,
        "https://example.test/old2",
        "thoroughbred",
        2,
        "Flemington",
        "R2",
        "Sarah's Sonnets",
        backup="Dracena",
        pick_data={
            "pick_name": "Sarah's Sonnets",
            "backup": "Dracena",
            "snapshot": {
                "field": [
                    {"name": "Sarah's Sonnets", "program_number": 5, "draw": 12},
                    {"name": "Dracena", "program_number": 4, "draw": 1},
                ]
            },
        },
        db_path=db,
    )
    stored = get_pick(d, "https://example.test/old2", 2, db_path=db)
    assert saved_pick_number(stored, "primary") == 5
    assert saved_pick_number(stored, "backup") == 4


def test_history_uses_saved_numbers_rather_than_live_data():
    pick = {
        "pick_name": "Sarah's Sonnets",
        "backup": "Dracena",
        "primary_number": 5,
        "backup_number": 4,
        "primary_odds": 4.8,
        "snapshot": {
            "primary_number": 5,
            "backup_number": 4,
            "field": [
                {"name": "Sarah's Sonnets", "program_number": 5, "draw": 12},
            ],
        },
    }
    live = SimpleNamespace(name="Sarah's Sonnets", draw=12, program_number=9, raw={})
    assert program_number_for_runner(live) == 9
    assert saved_pick_number(pick, "primary") == 5
    assert format_saved_selection(pick, "primary") == "5. SARAH'S SONNETS · $4.80"
    assert format_saved_selection(pick, "backup") == "4. DRACENA"


def test_legacy_runner_object_without_program_number_field():
    legacy = SimpleNamespace(
        name="Old Horse",
        draw=11,
        raw={"headers": ["Horse", "Barrier"], "cells": ["Old Horse", "11"]},
    )
    assert not hasattr(legacy, "program_number")
    assert program_number_for_runner(legacy) is None
