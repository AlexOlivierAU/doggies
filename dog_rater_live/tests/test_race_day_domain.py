from __future__ import annotations

from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from models import Meeting, Race, Runner
from race_db import (
    _conn,
    get_pick,
    load_picks,
    load_results,
    persist_results,
    save_pick,
)
from services.confidence import LABEL_CLOSE, LABEL_MEDIUM, LABEL_STRONG, confidence_from_scores, confidence_label
from services.formatting import ordinal
from services.names import names_match, normalize_runner_name
from services.pick_service import (
    build_snapshot_payload,
    confirm_pick,
    maybe_autolock,
    record_primary_scratching,
    save_selection_snapshot,
)
from services.race_day_service import build_race_views, jump_datetime, next_to_jump, upcoming_races
from services.result_service import (
    AWAITING_RESULT,
    BACKUP_WON,
    LOST,
    PENDING,
    PLACED,
    PRIMARY_SCRATCHED,
    RESULT_UNAVAILABLE,
    VOID,
    WIN,
    daily_summary,
    resolve_pick_result,
)


SYD = ZoneInfo("Australia/Sydney")


def _dt(h: int, m: int = 0) -> datetime:
    return datetime(2026, 8, 29, h, m, tzinfo=SYD)


def _runner(name: str, draw: int = 1, scratched: bool = False, finishes: list[int] | None = None, program_number: int | None = None) -> Runner:
    return Runner(
        code="thoroughbred",
        name=name,
        draw=draw,
        recent_finishes=finishes or [2, 3, 1, 4],
        early_speed=None,
        scratched=scratched,
        weight_kg=56.0 + draw,
        benchmark=60.0 + draw,
        program_number=program_number,
    )


def _meeting(venue: str, state: str, url: str) -> Meeting:
    return Meeting(
        code="thoroughbred",
        source="racingaustralia",
        venue=venue,
        meeting_date=date(2026, 8, 29),
        first_race_time_local=time(12, 0),
        num_races=3,
        meeting_url=url,
        status="upcoming",
        extra={"state": state, "key": f"2026Aug29,{state},{venue}"},
    )


def _race(n: int, hh: int, mm: int) -> Race:
    return Race(
        code="thoroughbred",
        race_no=n,
        name=f"BENCHMARK 64 HANDICAP R{n}",
        distance_m=1200 + n * 100,
        start_time_local=time(hh, mm),
        race_url=f"https://example.test/r{n}",
        extra={},
    )


# --- Confidence ---


def test_confidence_label_thresholds():
    assert confidence_label(0.05) == LABEL_STRONG
    assert confidence_label(0.08) == LABEL_STRONG
    assert confidence_label(0.02) == LABEL_MEDIUM
    assert confidence_label(0.049) == LABEL_MEDIUM
    assert confidence_label(0.019) == LABEL_CLOSE
    assert confidence_label(0.0) == LABEL_CLOSE
    gap, label = confidence_from_scores(0.72, 0.64)
    assert gap == pytest.approx(0.08)
    assert label == LABEL_STRONG


# --- Names ---


def test_runner_name_normalisation_strips_country_and_noise():
    assert normalize_runner_name("King's Gambit (NZ)") == normalize_runner_name("King's Gambit")
    assert normalize_runner_name("  VIA SISTINA  ") == "via sistina"
    assert names_match("Via Sistina (IRE)", "VIA SISTINA")
    assert not names_match("Via Sistina", "Via Sistina Too")  # no substring assignment
    assert not names_match("", "Horse")
    assert not names_match("A", "A")  # too short to be matchable


# --- Snapshot persistence / lock ---


def test_selection_snapshot_persists_and_does_not_overwrite_when_locked(tmp_path: Path):
    db = tmp_path / "roster.db"
    d = date(2026, 8, 29)
    payload = build_snapshot_payload(
        meeting_date=d,
        code="thoroughbred",
        venue="Randwick",
        meeting_url="https://example.test/randwick",
        race_no=3,
        race_name="BM64",
        race_url="https://example.test/r3",
        primary="King's Gambit",
        backup="Via Sistina",
        primary_score=0.71,
        backup_score=0.62,
        primary_odds=4.2,
        backup_odds=7.5,
        scheduled_jump="2026-08-29T13:15:00+10:00",
        weights={"draw": 0.2, "form": 0.5, "proxy": 0.3},
        field=[{"name": "King's Gambit", "draw": 4, "scratched": False}],
        why_bullets=["recent form (strong)"],
    )
    ok = save_selection_snapshot(
        meeting_date=d,
        meeting_url="https://example.test/randwick",
        code="thoroughbred",
        race_no=3,
        venue="Randwick",
        race_label="R3",
        primary="King's Gambit",
        backup="Via Sistina",
        pick_data=payload,
        best_score=0.71,
        backup_score=0.62,
        primary_odds=4.2,
        lock=True,
        db_path=db,
    )
    assert ok
    stored = get_pick(d, "https://example.test/randwick", 3, db_path=db)
    assert stored is not None
    assert stored["locked"] is True
    assert stored["pick_name"] == "King's Gambit"
    assert stored["backup"] == "Via Sistina"
    assert stored["confidence_label"] == LABEL_STRONG
    assert stored["primary_odds"] == pytest.approx(4.2)
    snap = (stored.get("snapshot") or {})
    assert snap.get("weights") == {"draw": 0.2, "form": 0.5, "proxy": 0.3}

    blocked = save_selection_snapshot(
        meeting_date=d,
        meeting_url="https://example.test/randwick",
        code="thoroughbred",
        race_no=3,
        venue="Randwick",
        race_label="R3",
        primary="A Completely Different Horse",
        backup="Someone Else",
        best_score=0.99,
        db_path=db,
    )
    assert blocked is False
    again = get_pick(d, "https://example.test/randwick", 3, db_path=db)
    assert again["pick_name"] == "King's Gambit"
    assert again["backup"] == "Via Sistina"


def test_legacy_database_migration_keeps_existing_picks(tmp_path: Path):
    db = tmp_path / "roster.db"
    import sqlite3

    db.parent.mkdir(parents=True, exist_ok=True)
    raw = sqlite3.connect(str(db))
    raw.execute(
        """CREATE TABLE picks (
            date TEXT, meeting_url TEXT, code TEXT, race_no INTEGER,
            venue TEXT, race_label TEXT, best_pick TEXT, backup TEXT,
            pick_data BLOB, saved_at REAL,
            PRIMARY KEY (date, meeting_url, race_no)
        )"""
    )
    raw.execute(
        "INSERT INTO picks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "2026-08-01",
            "https://example.test/old",
            "thoroughbred",
            1,
            "Flemington",
            "R1",
            "Legacy Horse",
            "Legacy Backup",
            None,
            1.0,
        ),
    )
    raw.commit()
    raw.close()

    conn = _conn(db)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(picks)")}
    conn.close()
    assert "confidence_label" in cols
    assert "locked" in cols
    assert "primary_scratched" in cols
    assert "primary_number" in cols
    assert "backup_number" in cols
    picks = load_picks(date(2026, 8, 1), db_path=db)
    assert len(picks) == 1
    assert picks[0]["pick_name"] == "Legacy Horse"
    assert picks[0]["backup"] == "Legacy Backup"
    assert picks[0]["locked"] is False


# --- Result matching ---


def _pick(**kw) -> dict:
    base = {
        "venue": "Randwick",
        "race_no": 1,
        "pick_name": "King's Gambit",
        "backup": "Via Sistina",
        "primary_odds": 4.0,
        "confidence_label": LABEL_STRONG,
        "primary_scratched": False,
        "backup_promoted": False,
        "original_primary": "",
    }
    base.update(kw)
    return base


def test_primary_winner_matching():
    res = resolve_pick_result(
        _pick(),
        {"winner": "King's Gambit (NZ)", "place2": "Other", "place3": "Third"},
        jumped=True,
    )
    assert res.status == WIN
    assert res.primary_finish == 1
    assert res.primary_finish_label == "1st"


def test_primary_placed_matching():
    res = resolve_pick_result(
        _pick(),
        {"winner": "Other", "place2": "King's Gambit", "place3": "Third"},
        jumped=True,
    )
    assert res.status == PLACED
    assert res.primary_finish == 2
    assert res.primary_finish_label == "2nd"

    res3 = resolve_pick_result(
        _pick(),
        {"winner": "Other", "place2": "Second", "place3": "King's Gambit"},
        jumped=True,
    )
    assert res3.status == PLACED
    assert res3.primary_finish == 3


def test_backup_winner_matching_and_primary_scratching(tmp_path: Path):
    db = tmp_path / "roster.db"
    d = date(2026, 8, 29)
    save_pick(
        d,
        "https://example.test/r",
        "thoroughbred",
        2,
        "Randwick",
        "R2",
        "King's Gambit",
        backup="Via Sistina",
        db_path=db,
    )
    confirm_pick(d, "https://example.test/r", 2, db_path=db)
    updated = record_primary_scratching(d, "https://example.test/r", 2, db_path=db)
    assert updated is not None
    assert updated["pick_name"] == "King's Gambit"
    assert updated["original_primary"] == "King's Gambit"
    assert updated["primary_scratched"] is True
    assert updated["backup_promoted"] is True
    assert updated["backup"] == "Via Sistina"

    res = resolve_pick_result(
        updated,
        {"winner": "Via Sistina", "place2": "X", "place3": "Y"},
        jumped=True,
    )
    assert res.status == BACKUP_WON
    assert res.backup_finish == 1

    scratched_only = resolve_pick_result(
        updated,
        {"winner": "Someone Else", "place2": "X", "place3": "Y"},
        jumped=True,
    )
    assert scratched_only.status == PRIMARY_SCRATCHED


def test_pending_versus_awaiting_result_states():
    now = _dt(12, 0)
    jump = _dt(13, 0)
    pending = resolve_pick_result(_pick(), None, now=now, jump_at=jump)
    assert pending.status == PENDING

    awaiting = resolve_pick_result(_pick(), {"winner": ""}, now=_dt(13, 5), jump_at=jump)
    assert awaiting.status == AWAITING_RESULT


def test_result_unavailable_on_fetch_failure_and_uncertain_match():
    fail = resolve_pick_result(_pick(), None, jumped=True, fetch_failed=True)
    assert fail.status == RESULT_UNAVAILABLE
    assert fail.fetch_failed is True

    stored_error = resolve_pick_result(
        _pick(),
        {"winner": "", "status": "error", "error_message": "timeout"},
        jumped=True,
    )
    assert stored_error.status == RESULT_UNAVAILABLE

    incomplete = resolve_pick_result(
        _pick(),
        {"winner": "Completely Different", "place2": "", "place3": ""},
        jumped=True,
    )
    assert incomplete.status == RESULT_UNAVAILABLE

    lost = resolve_pick_result(
        _pick(),
        {"winner": "Alpha", "place2": "Beta", "place3": "Gamma"},
        jumped=True,
    )
    assert lost.status == LOST
    assert lost.primary_finish_label == "unplaced"

    voided = resolve_pick_result(_pick(), {"status": "void"}, jumped=True)
    assert voided.status == VOID


def test_daily_summary_calculations():
    rows = [
        {"status": WIN, "primary_odds": 4.0, "confidence_label": LABEL_STRONG},
        {"status": WIN, "primary_odds": 3.0, "confidence_label": LABEL_STRONG},
        {"status": PLACED, "primary_odds": 5.0, "confidence_label": LABEL_MEDIUM},
        {"status": LOST, "primary_odds": 6.0, "confidence_label": LABEL_CLOSE},
        {"status": PENDING, "primary_odds": 2.0, "confidence_label": LABEL_STRONG},
        {"status": BACKUP_WON, "primary_odds": 8.0, "confidence_label": LABEL_MEDIUM},
        {"status": PRIMARY_SCRATCHED, "primary_odds": 4.0, "confidence_label": LABEL_STRONG},
    ]
    summary = daily_summary(rows)
    assert summary.completed == 4
    assert summary.primary_wins == 2
    assert summary.primary_places == 3
    assert summary.backup_wins == 1
    assert summary.win_strike_rate == pytest.approx(0.5)
    assert summary.place_strike_rate == pytest.approx(0.75)
    # 2 wins at 4.0 and 3.0 => +(3+2), two non-wins => -2, net +3
    assert summary.estimated_win_return == pytest.approx(3.0)

    missing_odds = daily_summary(
        [
            {"status": WIN, "primary_odds": None, "confidence_label": LABEL_STRONG},
            {"status": LOST, "primary_odds": 4.0, "confidence_label": LABEL_CLOSE},
        ]
    )
    assert missing_odds.estimated_win_return is None
    assert missing_odds.estimated_return_label == ""


def test_chronological_next_to_jump_ordering():
    chosen = date(2026, 8, 29)
    now = datetime(2026, 8, 29, 13, 0, tzinfo=SYD)
    randwick = _meeting("Randwick", "NSW", "https://example.test/randwick")
    eagle = _meeting("Eagle Farm", "QLD", "https://example.test/eagle")
    meetings = [eagle, randwick]
    fields = {
        randwick.meeting_url: {
            "races": [_race(1, 12, 0), _race(2, 13, 20), _race(3, 14, 0)],
            "runners_by_race": {
                1: [_runner("Old Horse", 1)],
                2: [_runner("Next Horse", 2, finishes=[1, 1, 2]), _runner("Backup Horse", 3, finishes=[4, 5, 6])],
                3: [_runner("Later Horse", 4)],
            },
            "meta": {"track_condition": "Good 4"},
        },
        eagle.meeting_url: {
            "races": [_race(1, 13, 10)],
            "runners_by_race": {
                1: [_runner("Qld Horse", 1, finishes=[1, 2, 3]), _runner("Qld Two", 5, finishes=[6, 7, 8])],
            },
            "meta": {"track_condition": "Soft 5"},
        },
    }
    views = build_race_views(
        chosen_date=chosen,
        meetings=meetings,
        fields_by_meeting=fields,
        now=now,
        app_tz=SYD,
        rank_upcoming_only=False,
    )
    times = [v.jump_at for v in views if v.jump_at is not None]
    assert times == sorted(times)

    nxt = next_to_jump(views, now)
    assert nxt is not None
    # QLD 13:10 AEST is 13:10 local Brisbane = 13:10 AEST in August (no DST in QLD, NSW is +10)
    # Eagle Farm 13:10 Australia/Brisbane vs Randwick 13:20 Australia/Sydney — both UTC+10 in August.
    assert nxt.venue_raw == "Eagle Farm"
    assert nxt.race_no == 1

    upcoming = upcoming_races(views, now, limit=5)
    assert all(r.jump_at and r.jump_at > now for r in upcoming)
    assert nxt.race_key not in {r.race_key for r in upcoming}
    finished = [v for v in views if v.status == "finished"]
    assert finished
    assert all(v.jump_at < now for v in finished)


def _hero_view(
    *,
    url: str,
    venue: str,
    raw: str,
    no: int,
    jump: datetime,
    status: str,
    state: str = "NSW",
):
    from services.race_day_service import RaceView

    return RaceView(
        meeting_url=url,
        race_url="",
        code="thoroughbred",
        venue=venue,
        venue_raw=raw,
        state=state,
        race_no=no,
        race_name=f"R{no}",
        race_class="",
        distance_m=1250,
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
        confidence_label=LABEL_MEDIUM,
        odds=None,
        backup_odds=None,
        field_size=8,
        scratching_warning=False,
        locked=False,
        from_snapshot=False,
        live_status=status,
    )


def test_next_to_jump_holds_while_race_running():
    jumping = _hero_view(
        url="https://example.test/cannon",
        venue="Cannon Park (QLD)",
        raw="Cannon Park",
        no=1,
        jump=datetime(2026, 8, 29, 13, 14, tzinfo=SYD),
        status="in_progress",
        state="QLD",
    )
    later = _hero_view(
        url="https://example.test/cannon",
        venue="Cannon Park (QLD)",
        raw="Cannon Park",
        no=2,
        jump=datetime(2026, 8, 29, 13, 19, tzinfo=SYD),
        status="upcoming",
        state="QLD",
    )
    now = datetime(2026, 8, 29, 13, 16, tzinfo=SYD)
    hero = next_to_jump([jumping, later], now)
    assert hero is jumping
    sticky = next_to_jump([jumping, later], now, sticky=jumping)
    assert sticky is jumping
    up = upcoming_races([jumping, later], now, limit=8)
    assert later in up
    assert jumping not in up


def test_next_to_jump_yields_when_next_is_imminent():
    jumping = _hero_view(
        url="https://example.test/cannon",
        venue="Cannon Park (QLD)",
        raw="Cannon Park",
        no=1,
        jump=datetime(2026, 8, 29, 13, 14, tzinfo=SYD),
        status="in_progress",
        state="QLD",
    )
    later = _hero_view(
        url="https://example.test/cannon",
        venue="Cannon Park (QLD)",
        raw="Cannon Park",
        no=2,
        jump=datetime(2026, 8, 29, 13, 16, tzinfo=SYD),
        status="upcoming",
        state="QLD",
    )
    now = datetime(2026, 8, 29, 13, 15, 30, tzinfo=SYD)
    assert next_to_jump([jumping, later], now) is later


def test_next_to_jump_prefers_metro_when_times_match():
    picnic = _hero_view(
        url="https://example.test/betoota",
        venue="Betoota (QLD)",
        raw="Betoota",
        no=1,
        jump=datetime(2026, 8, 29, 13, 30, tzinfo=SYD),
        status="upcoming",
        state="QLD",
    )
    rosehill = _hero_view(
        url="https://example.test/rosehill",
        venue="Rosehill Gardens (NSW)",
        raw="Rosehill Gardens",
        no=1,
        jump=datetime(2026, 8, 29, 13, 30, tzinfo=SYD),
        status="upcoming",
    )
    now = datetime(2026, 8, 29, 13, 27, tzinfo=SYD)
    assert next_to_jump([picnic, rosehill], now) is rosehill
    up = upcoming_races([picnic, rosehill], now, limit=8)
    assert up[0] is picnic


def test_next_to_jump_yields_to_upcoming_after_hold():
    jumped = _hero_view(
        url="https://example.test/kembla",
        venue="Kembla Grange (NSW)",
        raw="Kembla Grange",
        no=2,
        jump=datetime(2026, 8, 29, 13, 15, tzinfo=SYD),
        status="in_progress",
    )
    nxt = _hero_view(
        url="https://example.test/kembla",
        venue="Kembla Grange (NSW)",
        raw="Kembla Grange",
        no=3,
        jump=datetime(2026, 8, 29, 13, 26, tzinfo=SYD),
        status="upcoming",
    )
    now = datetime(2026, 8, 29, 13, 22, tzinfo=SYD)
    assert next_to_jump([jumped, nxt], now) is nxt


def test_next_to_jump_does_not_sticky_a_later_upcoming_race():
    """Screenshot: Mudgee was hero first; Wyong must still win once it is in views."""
    now = datetime(2026, 8, 30, 12, 24, tzinfo=SYD)
    wyong = _hero_view(
        url="https://example.test/wyong",
        venue="Wyong (NSW)",
        raw="Wyong",
        no=1,
        jump=datetime(2026, 8, 30, 12, 35, tzinfo=SYD),
        status="upcoming",
    )
    casterton = _hero_view(
        url="https://example.test/casterton",
        venue="Casterton (VIC)",
        raw="Casterton",
        no=1,
        jump=datetime(2026, 8, 30, 12, 40, tzinfo=SYD),
        status="upcoming",
        state="VIC",
    )
    mudgee = _hero_view(
        url="https://example.test/mudgee",
        venue="Mudgee (NSW)",
        raw="Mudgee",
        no=1,
        jump=datetime(2026, 8, 30, 13, 25, tzinfo=SYD),
        status="upcoming",
    )
    for row in (wyong, casterton, mudgee):
        assert row.jump_at is not None and row.jump_at.tzinfo is not None
        assert row.jump_at.date() == date(2026, 8, 30)

    from services.race_day_service import derive_race_day_state

    state = derive_race_day_state([mudgee, casterton, wyong], now, sticky=mudgee, limit=12)
    assert state.hero is wyong
    assert wyong.race_key not in {r.race_key for r in state.upcoming}
    assert state.upcoming[0] is casterton
    assert mudgee in state.upcoming
    assert next_to_jump([mudgee, wyong], now, sticky=mudgee) is wyong
    from services.formatting import format_countdown

    now = datetime(2026, 8, 29, 13, 22, 19, tzinfo=SYD)
    assert format_countdown(datetime(2026, 8, 29, 13, 26, tzinfo=SYD), now) == "4m"
    assert format_countdown(datetime(2026, 8, 29, 13, 30, tzinfo=SYD), now) == "8m"
    assert format_countdown(datetime(2026, 8, 29, 13, 15, tzinfo=SYD), now) == "Jumped"


def test_autolock_near_jump_preserves_snapshot(tmp_path: Path):
    db = tmp_path / "roster.db"
    d = date(2026, 8, 29)
    save_selection_snapshot(
        meeting_date=d,
        meeting_url="https://example.test/r",
        code="thoroughbred",
        race_no=4,
        venue="Caulfield",
        race_label="R4",
        primary="Locked In",
        backup="Spare",
        best_score=0.6,
        backup_score=0.55,
        db_path=db,
    )
    pick = get_pick(d, "https://example.test/r", 4, db_path=db)
    now = _dt(13, 0)
    jump = _dt(13, 1)
    locked = maybe_autolock(pick, now=now, jump_at=jump, db_path=db)
    assert locked["locked"] is True
    save_selection_snapshot(
        meeting_date=d,
        meeting_url="https://example.test/r",
        code="thoroughbred",
        race_no=4,
        venue="Caulfield",
        race_label="R4",
        primary="Should Not Replace",
        backup="Nope",
        db_path=db,
    )
    final = get_pick(d, "https://example.test/r", 4, db_path=db)
    assert final["pick_name"] == "Locked In"


def test_ordinal_labels():
    assert ordinal(1) == "1st"
    assert ordinal(2) == "2nd"
    assert ordinal(3) == "3rd"
    assert ordinal(4) == "4th"
    assert ordinal(None) == "—"


def test_sync_missing_results_records_fetch_failure(tmp_path: Path):
    from services.race_day_service import RaceView
    from services.result_service import sync_missing_results
    from race_db import persist_result_failure, load_results, persist_results

    db = tmp_path / "roster.db"
    d = date(2026, 8, 29)
    now = _dt(14, 0)
    view = RaceView(
        meeting_url="https://example.test/m",
        race_url="",
        code="thoroughbred",
        venue="Randwick",
        venue_raw="Randwick",
        state="NSW",
        race_no=1,
        race_name="BM64",
        race_class="BM64",
        distance_m=1200,
        track_condition="Good4",
        jump_at=_dt(13, 0),
        status="finished",
        primary="A",
        primary_no="1",
        backup="B",
        backup_no="2",
        primary_score=0.6,
        backup_score=0.5,
        score_gap=0.1,
        confidence_label=LABEL_STRONG,
        odds=4.0,
        backup_odds=None,
        field_size=8,
        scratching_warning=False,
        locked=True,
        from_snapshot=True,
        live_status="finished",
    )

    def boom(code, url):
        raise RuntimeError("network down")

    out = sync_missing_results(
        chosen_date=d,
        views=[view],
        now=now,
        fetch_meeting_results=boom,
        load_results_fn=lambda *a, **k: load_results(*a, **k),
        persist_results_fn=lambda *a, **k: persist_results(*a, **k),
        persist_failure_fn=lambda *a, **k: persist_result_failure(*a, **k),
        db_path=db,
    )
    assert out["errors"] == 1
    stored = load_results(d, "https://example.test/m", "thoroughbred", db_path=db)
    assert stored[1]["status"] == "error"


def test_sync_missing_results_persists_winner(tmp_path: Path):
    from services.race_day_service import RaceView
    from services.result_service import sync_missing_results
    from race_db import persist_result_failure, load_results, persist_results

    db = tmp_path / "roster.db"
    d = date(2026, 8, 29)
    view = RaceView(
        meeting_url="https://example.test/m2",
        race_url="",
        code="thoroughbred",
        venue="Caulfield",
        venue_raw="Caulfield",
        state="VIC",
        race_no=2,
        race_name="MDN",
        race_class="MDN",
        distance_m=1400,
        track_condition="",
        jump_at=_dt(12, 0),
        status="finished",
        primary="Alpha",
        primary_no="3",
        backup="Beta",
        backup_no="4",
        primary_score=0.5,
        backup_score=0.4,
        score_gap=0.1,
        confidence_label=LABEL_STRONG,
        odds=None,
        backup_odds=None,
        field_size=10,
        scratching_warning=False,
        locked=True,
        from_snapshot=True,
        live_status="finished",
    )

    def fetch(code, url):
        return {2: {"winner": "Alpha", "place2": "Beta", "place3": "Gamma"}}

    sync_missing_results(
        chosen_date=d,
        views=[view],
        now=_dt(15, 0),
        fetch_meeting_results=fetch,
        load_results_fn=lambda *a, **k: load_results(*a, **k),
        persist_results_fn=lambda *a, **k: persist_results(*a, **k),
        persist_failure_fn=lambda *a, **k: persist_result_failure(*a, **k),
        db_path=db,
    )
    stored = load_results(d, "https://example.test/m2", "thoroughbred", db_path=db)
    assert stored[2]["winner"] == "Alpha"


def test_persist_results_roundtrip(tmp_path: Path):
    db = tmp_path / "roster.db"
    d = date(2026, 8, 29)
    persist_results(
        d,
        "https://example.test/m",
        "thoroughbred",
        {1: {"winner": "Alpha", "place2": "Beta", "place3": "Gamma", "source_url": "https://example.test/res"}},
        db_path=db,
    )
    stored = load_results(d, "https://example.test/m", "thoroughbred", db_path=db)
    assert stored[1]["winner"] == "Alpha"
    assert stored[1]["place2"] == "Beta"
    assert stored[1]["status"] == "ok"


def test_jump_datetime_orders_by_instant_not_clock_string():
    """NSW/VIC/SA on DST, QLD not — same local HH:MM is not the same instant."""
    chosen = date(2026, 10, 4)
    app_tz = ZoneInfo("Australia/Sydney")
    now = datetime(2026, 10, 4, 11, 0, tzinfo=app_tz)
    assert now.tzinfo is not None

    def mtg(venue: str, state: str, url: str) -> Meeting:
        return Meeting(
            code="thoroughbred",
            source="racingaustralia",
            venue=venue,
            meeting_date=chosen,
            first_race_time_local=time(12, 0),
            num_races=1,
            meeting_url=url,
            status="upcoming",
            extra={"state": state, "key": f"2026Oct04,{state},{venue}"},
        )

    gawler = mtg("Gawler", "SA", "https://example.test/gawler")
    wyong = mtg("Wyong", "NSW", "https://example.test/wyong")
    casterton = mtg("Casterton", "VIC", "https://example.test/casterton")
    toowoomba = mtg("Toowoomba", "QLD", "https://example.test/toowoomba")

    jumps = {
        "gawler": jump_datetime(chosen_date=chosen, start_time_local=time(12, 0), meeting=gawler, app_tz=app_tz),
        "wyong": jump_datetime(chosen_date=chosen, start_time_local=time(12, 35), meeting=wyong, app_tz=app_tz),
        "casterton": jump_datetime(chosen_date=chosen, start_time_local=time(13, 0), meeting=casterton, app_tz=app_tz),
        "toowoomba": jump_datetime(chosen_date=chosen, start_time_local=time(12, 40), meeting=toowoomba, app_tz=app_tz),
    }
    for label, dt in jumps.items():
        assert dt is not None, label
        assert dt.tzinfo is not None, label
        assert dt.astimezone(app_tz).date() == chosen, label

    # Lexicographic local clocks would put Toowoomba 12:40 before Casterton 13:00.
    assert jumps["gawler"] < jumps["wyong"] < jumps["casterton"] < jumps["toowoomba"]
    assert jumps["toowoomba"].strftime("%H:%M") != jumps["wyong"].strftime("%H:%M") or jumps["toowoomba"] != jumps["wyong"]

    from services.race_day_service import RaceView, derive_race_day_state

    def rv(name: str, meeting: Meeting, jump: datetime) -> RaceView:
        return _hero_view(
            url=meeting.meeting_url,
            venue=f"{name} ({meeting.extra['state']})",
            raw=name,
            no=1,
            jump=jump,
            status="upcoming",
            state=str(meeting.extra["state"]),
        )

    views = [
        rv("Toowoomba", toowoomba, jumps["toowoomba"]),
        rv("Casterton", casterton, jumps["casterton"]),
        rv("Wyong", wyong, jumps["wyong"]),
        rv("Gawler", gawler, jumps["gawler"]),
    ]
    state = derive_race_day_state(views, now, limit=12)
    assert state.hero is not None
    assert state.hero.venue_raw == "Gawler"
    assert [v.venue_raw for v in state.upcoming] == ["Wyong", "Casterton", "Toowoomba"]
    future = [v for v in views if v.jump_at is not None and v.jump_at > now]
    earliest = min(future, key=lambda v: v.jump_at)
    assert state.hero.jump_at == earliest.jump_at
    assert all(v.jump_at.tzinfo is not None for v in future)

