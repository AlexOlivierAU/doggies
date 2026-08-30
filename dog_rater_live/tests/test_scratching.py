from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from models import Meeting, Race, Runner
from race_db import get_pick
from services.pick_service import apply_view_scratching, build_snapshot_payload, save_selection_snapshot
from services.race_day_service import build_race_views
from services.result_service import BACKUP_PROMOTED, BOTH_SCRATCHED, NO_ACTIVE_SELECTION, PRIMARY_SCRATCHED, resolve_pick_result
from services.scratching import log_late_scratch_transition, reset_logged_transitions

SYD = ZoneInfo("Australia/Sydney")
NOW = datetime(2026, 8, 29, 12, 0, tzinfo=SYD)
URL = "https://example.test/casterton"


def _runner(name, draw, program_number, *, scratched=False, finishes=None):
    return Runner(
        code="thoroughbred",
        name=name,
        draw=draw,
        recent_finishes=finishes or [1, 2, 3],
        early_speed=None,
        scratched=scratched,
        weight_kg=56.0,
        benchmark=64.0,
        program_number=program_number,
    )


def _field():
    return [
        _runner("QUYNH", 1, 12, finishes=[1, 1, 1, 1]),
        _runner("PIKLEMEGRANDMOTHER", 2, 10, finishes=[2, 2, 3, 4]),
        _runner("THIRD HORSE", 3, 8, finishes=[8, 8, 8, 8]),
    ]


def _odds_rows(*, quynh_out=True, extra=None):
    rows = [
        {"name": "QUYNH", "no": 12, "scratched": quynh_out, "win": None if quynh_out else 3.2, "source": "sportsbet"},
        {"name": "PIKLEMEGRANDMOTHER", "no": 10, "scratched": False, "win": 6.5, "source": "sportsbet"},
        {"name": "THIRD HORSE", "no": 8, "scratched": False, "win": 9.0, "source": "sportsbet"},
    ]
    if extra:
        rows.extend(extra)
    return rows


def _meeting():
    return Meeting(
        code="thoroughbred",
        source="racingaustralia",
        venue="Casterton",
        meeting_date=date(2026, 8, 29),
        first_race_time_local=time(12, 35),
        num_races=2,
        meeting_url=URL,
        status="upcoming",
        extra={"state": "VIC", "key": "2026Aug29,VIC,Casterton"},
    )


def _views(*, runners=None, odds_rows=None, saved=None, now=NOW, jump=None, persisted=None):
    meeting = _meeting()
    race = Race("thoroughbred", 2, "R2", 1200, time(12, 35) if jump is None else jump, URL + "/r2", {})
    fields = {
        URL: {
            "races": [race],
            "runners_by_race": {2: runners or _field()},
            "meta": {"track_condition": "Good 4"},
        }
    }
    rows = odds_rows if odds_rows is not None else _odds_rows()

    def lookup(venue, race_no, horse):
        from services.names import names_match

        for row in rows:
            if names_match(row.get("name"), horse):
                return row
        return None

    def rows_lookup(venue, race_no):
        return list(rows)

    saved_picks = dict(saved or {})
    if persisted:
        saved_picks[(URL, 2)] = persisted
    return build_race_views(
        chosen_date=date(2026, 8, 29),
        meetings=[meeting],
        fields_by_meeting=fields,
        now=now,
        app_tz=SYD,
        saved_picks=saved_picks,
        odds_lookup=lookup,
        odds_rows_lookup=rows_lookup,
    )


def _lock(view, db_path):
    payload = build_snapshot_payload(
        meeting_date=date(2026, 8, 29),
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
        primary_odds=view.odds,
        backup_odds=view.backup_odds,
        primary_number=view.primary_no,
        backup_number=view.backup_no,
        scheduled_jump=view.jump_at.isoformat() if view.jump_at else "",
        field_size=view.field_size,
    )
    save_selection_snapshot(
        meeting_date=date(2026, 8, 29),
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
        primary_odds=view.odds,
        backup_odds=view.backup_odds,
        lock=True,
        db_path=db_path,
    )
    return get_pick(date(2026, 8, 29), URL, 2, db_path=db_path)


def test_sportsbet_primary_scratched_before_ranking():
    views = _views()
    assert len(views) == 1
    v = views[0]
    names = [r.name for r in v.runners]
    assert "QUYNH" in names
    quynh = next(r for r in v.runners if r.name == "QUYNH")
    assert quynh.scratched is True
    ranked_names = [r.name for r in v.ranked]
    assert "QUYNH" not in ranked_names
    assert v.primary == "PIKLEMEGRANDMOTHER"
    assert v.primary_no == "10"
    assert v.backup == "THIRD HORSE"
    assert v.backup_no == "8"
    assert v.active_primary == "PIKLEMEGRANDMOTHER"
    assert v.field_size == 2
    assert v.no_active_selection is False


def test_sportsbet_is_out_flag_is_sufficient():
    rows = [
        {"name": "QUYNH", "no": 12, "isOut": True, "source": "sportsbet"},
        {"name": "PIKLEMEGRANDMOTHER", "no": 10, "scratched": False, "win": 6.5, "source": "sportsbet"},
        {"name": "THIRD HORSE", "no": 8, "scratched": False, "win": 9.0, "source": "sportsbet"},
    ]
    views = _views(odds_rows=rows)
    v = views[0]
    assert v.primary != "QUYNH"
    assert next(r for r in v.runners if r.name == "QUYNH").scratched is True


def test_does_not_mutate_parser_owned_runners():
    runners = _field()
    _views(runners=runners)
    assert runners[0].name == "QUYNH"
    assert runners[0].scratched is False


def test_missing_odds_is_not_scratching():
    views = _views(odds_rows=_odds_rows(quynh_out=False)[1:])
    v = views[0]
    quynh = next(r for r in v.runners if r.name == "QUYNH")
    assert quynh.scratched is False
    assert v.primary == "QUYNH"
    assert v.odds is None


def test_parser_scratching_without_sportsbet():
    runners = [replace(_field()[0], scratched=True), *_field()[1:]]
    views = _views(runners=runners, odds_rows=[])
    v = views[0]
    assert next(r for r in v.runners if r.name == "QUYNH").scratched is True
    assert v.primary == "PIKLEMEGRANDMOTHER"
    assert "QUYNH" not in [r.name for r in v.ranked]


def test_locked_primary_late_scratching(tmp_path):
    db = tmp_path / "roster.db"
    unlocked = _views(odds_rows=_odds_rows(quynh_out=False))[0]
    assert unlocked.primary == "QUYNH"
    snap = _lock(unlocked, db)
    assert snap["pick_name"] == "QUYNH"
    views = _views(odds_rows=_odds_rows(quynh_out=True), persisted=snap)
    v = views[0]
    assert v.from_snapshot is True
    assert v.original_primary == "QUYNH"
    assert v.primary_scratched is True
    assert v.backup_promoted is True
    assert v.primary == "PIKLEMEGRANDMOTHER"
    assert v.active_primary == "PIKLEMEGRANDMOTHER"
    assert v.backup == "THIRD HORSE"
    apply_view_scratching(v, chosen_date=date(2026, 8, 29), db_path=db, now=NOW)
    stored = get_pick(date(2026, 8, 29), URL, 2, db_path=db)
    assert stored["pick_name"] == "QUYNH"
    assert stored["primary_scratched"] is True
    assert stored["backup_promoted"] is True
    assert stored["active_primary"] == "PIKLEMEGRANDMOTHER"
    resolved = resolve_pick_result(stored, None, jumped=False)
    assert resolved.status == BACKUP_PROMOTED


def test_backup_scratched_replaced():
    rows = _odds_rows(quynh_out=False)
    rows[1]["scratched"] = True
    views = _views(odds_rows=rows)
    v = views[0]
    assert v.primary == "QUYNH"
    assert v.backup == "THIRD HORSE"


def test_primary_and_backup_both_scratched_locked(tmp_path):
    db = tmp_path / "roster.db"
    unlocked = _views(odds_rows=_odds_rows(quynh_out=False))[0]
    snap = _lock(unlocked, db)
    rows = _odds_rows(quynh_out=True)
    rows[1]["scratched"] = True
    views = _views(odds_rows=rows, persisted=snap)
    v = views[0]
    assert v.original_primary == "QUYNH"
    assert v.original_backup == "PIKLEMEGRANDMOTHER"
    assert v.primary_scratched is True
    assert v.backup_scratched is True
    assert v.backup_promoted is False
    assert v.primary == "THIRD HORSE"
    assert v.active_primary == "THIRD HORSE"
    apply_view_scratching(v, chosen_date=date(2026, 8, 29), db_path=db, now=NOW)
    stored = get_pick(date(2026, 8, 29), URL, 2, db_path=db)
    resolved = resolve_pick_result(stored, None, jumped=False)
    assert resolved.status == BOTH_SCRATCHED


def test_no_active_runners_disables_selection():
    rows = [
        {"name": "QUYNH", "no": 12, "scratched": True, "source": "sportsbet"},
        {"name": "PIKLEMEGRANDMOTHER", "no": 10, "scratched": True, "source": "sportsbet"},
        {"name": "THIRD HORSE", "no": 8, "scratched": True, "source": "sportsbet"},
    ]
    views = _views(odds_rows=rows)
    v = views[0]
    assert v.no_active_selection is True
    assert not v.primary
    assert not v.backup
    assert v.field_size == 0
    assert v.selection_warning
    resolved = resolve_pick_result(
        {"primary_scratched": True, "backup_scratched": True, "pick_name": "QUYNH"},
        None,
        jumped=False,
    )
    assert resolved.status in {BOTH_SCRATCHED, NO_ACTIVE_SELECTION, PRIMARY_SCRATCHED}


def test_ambiguous_or_unmatched_sportsbet_does_not_scratch_wrong_horse():
    rows = [
        {"name": "QUYNH STAR", "no": 99, "scratched": True, "source": "sportsbet"},
        {"name": "PIKLEMEGRANDMOTHER", "no": 10, "scratched": False, "win": 6.5, "source": "sportsbet"},
        {"name": "THIRD HORSE", "no": 8, "scratched": False, "win": 9.0, "source": "sportsbet"},
    ]
    views = _views(odds_rows=rows)
    v = views[0]
    assert next(r for r in v.runners if r.name == "QUYNH").scratched is False
    assert v.primary == "QUYNH"

    mismatch = _odds_rows(quynh_out=True)
    mismatch[0]["no"] = 99
    views2 = _views(odds_rows=mismatch)
    v2 = views2[0]
    assert next(r for r in v2.runners if r.name == "QUYNH").scratched is False
    assert v2.primary == "QUYNH"


def test_idempotent_late_scratch_logging(caplog):
    reset_logged_transitions()
    caplog.set_level("INFO")
    payload = dict(
        venue="Casterton",
        race_no=2,
        horse="QUYNH",
        source="sportsbet",
        locked=False,
        new_primary="PIKLEMEGRANDMOTHER",
        new_backup="THIRD HORSE",
    )
    log_late_scratch_transition(**payload)
    log_late_scratch_transition(**payload)
    lines = [r.message for r in caplog.records if "Late scratching" in r.message]
    assert len(lines) == 1


def test_persisted_scratch_does_not_revert_when_odds_omit_row():
    persisted = {
        "best_pick": "QUYNH",
        "backup": "PIKLEMEGRANDMOTHER",
        "primary_number": 12,
        "backup_number": 10,
        "locked": 0,
        "primary_scratched": True,
        "backup_scratched": False,
        "scratching_source": "sportsbet",
        "scratching_detected_at": "2026-08-29T12:05:00+10:00",
        "backup_promoted": True,
        "active_primary": "PIKLEMEGRANDMOTHER",
        "active_backup": "THIRD HORSE",
    }
    remaining = _odds_rows(quynh_out=False)[1:]
    views = _views(odds_rows=remaining, persisted=persisted)
    v = views[0]
    quynh = next(r for r in v.runners if r.name == "QUYNH")
    assert quynh.scratched is True
    assert v.primary != "QUYNH"


def test_in_progress_preserves_original_and_does_not_invent_post_result_pick():
    unlocked = _views(odds_rows=_odds_rows(quynh_out=False))[0]
    snap = {
        "best_pick": unlocked.primary,
        "backup": unlocked.backup,
        "primary_number": unlocked.primary_no,
        "backup_number": unlocked.backup_no,
        "locked": 1,
        "locked_at": "2026-08-29T12:30:00+10:00",
        "status": "WIN",
        "original_primary": unlocked.primary,
    }
    after = datetime(2026, 8, 29, 14, 0, tzinfo=SYD)
    views = _views(odds_rows=_odds_rows(quynh_out=True), persisted=snap, now=after)
    v = views[0]
    assert v.original_primary == unlocked.primary
    assert v.from_snapshot is True
    assert v.primary_scratched is True
