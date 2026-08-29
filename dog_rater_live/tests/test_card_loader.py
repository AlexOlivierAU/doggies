from __future__ import annotations

from datetime import date
from pathlib import Path

from models import Meeting, Race, Runner
from race_db import persist_daily_fields, persist_daily_meetings
from services.card_loader import MEETINGS_CODE, merge_fields_maps, refresh_card


def _meeting(url="https://example.test/m") -> Meeting:
    return Meeting(
        code="thoroughbred",
        source="racingaustralia",
        venue="Randwick",
        meeting_date=date(2026, 8, 29),
        first_race_time_local=None,
        num_races=1,
        meeting_url=url,
        status="upcoming",
        extra={"state": "NSW"},
    )


def _fields() -> dict:
    race = Race(
        code="thoroughbred",
        race_no=1,
        name="BM64",
        distance_m=1200,
        start_time_local=None,
        race_url="https://example.test/r1",
        extra={},
    )
    runner = Runner(
        code="thoroughbred",
        name="Sarah's Sonnets",
        draw=12,
        recent_finishes=[1, 2],
        early_speed=None,
        program_number=5,
    )
    return {
        "https://example.test/m": {
            "races": [race],
            "runners_by_race": {1: [runner]},
            "meta": {"track_condition": "Good 4"},
        }
    }


def test_merge_fields_does_not_wipe_populated_card():
    base = _fields()
    empty = {"https://example.test/m": {"races": [], "runners_by_race": {}, "meta": {}}}
    merged = merge_fields_maps(base, empty)
    assert merged["https://example.test/m"]["races"]
    assert merged["https://example.test/m"]["runners_by_race"][1][0].program_number == 5
    assert merged["https://example.test/m"]["runners_by_race"][1][0].draw == 12


def test_failed_live_refresh_keeps_cached_card(tmp_path: Path, monkeypatch):
    d = date(2026, 8, 29)
    db = tmp_path / "roster.db"
    meetings = [_meeting()]
    fields = _fields()
    persist_daily_meetings(d, MEETINGS_CODE, meetings, db_path=db)
    persist_daily_fields(
        d,
        meetings[0].meeting_url,
        (fields[meetings[0].meeting_url]["races"], fields[meetings[0].meeting_url]["runners_by_race"], {}),
        db_path=db,
    )

    def boom(*_a, **_k):
        raise RuntimeError("network down")

    monkeypatch.setattr("services.card_loader.fetch_tb_meetings", boom)
    monkeypatch.setattr("services.card_loader.fetch_tb_fields", boom)
    payload = refresh_card(
        d,
        previous_meetings=meetings,
        previous_fields=fields,
        db_path=db,
        live=True,
        force=True,
    )
    assert payload.status in {"cached", "partial", "failure"}
    assert payload.meetings
    assert payload.fields_by_meeting[meetings[0].meeting_url]["runners_by_race"][1]


def test_refresh_without_live_uses_db(tmp_path: Path):
    d = date(2026, 8, 29)
    db = tmp_path / "roster.db"
    meetings = [_meeting()]
    fields = _fields()
    persist_daily_meetings(d, MEETINGS_CODE, meetings, db_path=db)
    persist_daily_fields(
        d,
        meetings[0].meeting_url,
        (fields[meetings[0].meeting_url]["races"], fields[meetings[0].meeting_url]["runners_by_race"], {}),
        db_path=db,
    )
    payload = refresh_card(d, db_path=db, live=False)
    assert payload.meetings
    assert payload.status == "cached"
