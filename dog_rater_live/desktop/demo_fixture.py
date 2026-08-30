"""Deterministic grids for screenshots and --demo-grids. No live network."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from models import Runner
from services.race_day_service import RaceView
from services.result_service import AWAITING_RESULT, BACKUP_WON, LOST, WIN

from desktop.models.race_table_model import race_to_row
from desktop.table_theme import result_tone

SYD = ZoneInfo("Australia/Sydney")
NOW = datetime(2026, 8, 29, 12, 24, tzinfo=SYD)


def _runner(name, draw, program_number, *, silk="", jockey="", trainer="", last10="1-2-3", weight=56.0, scratched=False):
    return Runner(
        code="thoroughbred",
        name=name,
        draw=draw,
        recent_finishes=[1, 2, 3],
        early_speed=None,
        program_number=program_number,
        weight_kg=weight,
        benchmark=64.0,
        jockey_or_driver=jockey or "J. Demo",
        trainer=trainer or "T. Demo",
        last10=last10,
        silk_url=silk,
        scratched=scratched,
        profile_url="https://example.test/horse",
    )


def demo_view(**kw) -> RaceView:
    runners = [
        _runner("Sarah's Sonnets", 12, 5, jockey="T. Clark", trainer="C. Waller", last10="1-2-3-4"),
        _runner("Dracena", 1, 4, jockey="J. Collett", trainer="J. Cummings", last10="2-1-5"),
        _runner("Quiet Storm", 6, 8, jockey="K. McEvoy", trainer="G. Waterhouse", last10="4-6-2", scratched=True),
    ]
    base = dict(
        meeting_url="https://example.test/wyong",
        race_url="https://example.test/wyong/r1",
        code="thoroughbred",
        venue="Wyong (NSW)",
        venue_raw="Wyong",
        state="NSW",
        race_no=1,
        race_name="BM64",
        race_class="BM64",
        distance_m=1200,
        track_condition="Good4",
        jump_at=datetime(2026, 8, 29, 12, 35, tzinfo=SYD),
        status="upcoming",
        primary="Sarah's Sonnets",
        primary_no="5",
        backup="Dracena",
        backup_no="4",
        primary_score=0.72,
        backup_score=0.61,
        score_gap=0.11,
        confidence_label="Strong",
        odds=4.8,
        backup_odds=11.0,
        field_size=8,
        scratching_warning=True,
        locked=False,
        from_snapshot=False,
        live_status="upcoming",
        runners=runners,
        meta={"track_condition": "Good4"},
        why=["Form in last three", "Draw suits tempo"],
    )
    base.update(kw)
    return RaceView(**base)


def demo_upcoming_views() -> list[RaceView]:
    return [
        demo_view(),
        demo_view(
            meeting_url="https://example.test/randwick",
            race_url="https://example.test/randwick/r3",
            venue="Randwick (NSW)",
            venue_raw="Randwick",
            race_no=3,
            jump_at=datetime(2026, 8, 29, 13, 20, tzinfo=SYD),
            primary="Later Horse",
            primary_no="2",
            backup="Second Later",
            backup_no="7",
            odds=6.5,
            backup_odds=9.0,
            scratching_warning=False,
            runners=[
                _runner("Later Horse", 3, 2),
                _runner("Second Later", 8, 7),
            ],
        ),
        demo_view(
            meeting_url="https://example.test/casterton",
            race_url="https://example.test/casterton/r4",
            venue="Casterton (VIC)",
            venue_raw="Casterton",
            state="VIC",
            race_no=4,
            jump_at=datetime(2026, 8, 29, 13, 50, tzinfo=SYD),
            confidence_label="Close race",
            odds=3.2,
            backup_odds=8.5,
            scratching_warning=False,
        ),
    ]


def demo_pick_rows() -> list[dict]:
    rows = [
        {
            "result": WIN,
            "jump": "11:05",
            "venue": "Rosehill",
            "race": "R2",
            "primary": "3. ALPHA STAR",
            "primary_finish": "1st",
            "saved_odds": 4.2,
            "backup": "9. BRAVO",
            "backup_finish": "5th",
            "confidence": "Strong",
            "source": "official",
            "race_key": ("https://example.test/rosehill", 2),
            "primary_name": "Alpha Star",
            "backup_name": "Bravo",
            "primary_no": 3,
            "backup_no": 9,
        },
        {
            "result": BACKUP_WON,
            "jump": "11:40",
            "venue": "Rosehill",
            "race": "R3",
            "primary": "1. GAMMA",
            "primary_finish": "SCR",
            "saved_odds": 5.5,
            "backup": "2. DELTA",
            "backup_finish": "1st",
            "confidence": "Medium",
            "source": "official",
            "race_key": ("https://example.test/rosehill", 3),
            "primary_name": "Gamma",
            "backup_name": "Delta",
            "primary_no": 1,
            "backup_no": 2,
            "primary_scratched": True,
        },
        {
            "result": LOST,
            "jump": "12:10",
            "venue": "Flemington",
            "race": "R5",
            "primary": "6. ECHO",
            "primary_finish": "7th",
            "saved_odds": 7.0,
            "backup": "4. FOXTROT",
            "backup_finish": "4th",
            "confidence": "Close race",
            "source": "official",
            "race_key": ("https://example.test/flemington", 5),
            "primary_name": "Echo",
            "backup_name": "Foxtrot",
            "primary_no": 6,
            "backup_no": 4,
        },
        {
            "result": AWAITING_RESULT,
            "jump": "12:20",
            "venue": "Wyong",
            "race": "R8",
            "primary": "5. SARAH'S SONNETS",
            "primary_finish": "—",
            "saved_odds": 4.8,
            "backup": "4. DRACENA",
            "backup_finish": "—",
            "confidence": "Strong",
            "source": "awaiting",
            "race_key": ("https://example.test/wyong", 8),
            "primary_name": "Sarah's Sonnets",
            "backup_name": "Dracena",
            "primary_no": 5,
            "backup_no": 4,
        },
    ]
    for row in rows:
        row["row_tone"] = result_tone(row["result"])
        row["status"] = row["result"]
    return rows


def demo_runner_detail() -> dict:
    view = demo_view()
    runner = view.runners[0]
    return {
        "role": "primary",
        "name": runner.name,
        "raw_name": runner.name,
        "no": runner.program_number,
        "barrier": runner.draw,
        "odds": view.odds,
        "fluc": "↓",
        "form": runner.last10,
        "last_class": "BM58",
        "class_label": view.race_class,
        "class_arrow": "↑",
        "weight": runner.weight_kg,
        "jockey": runner.jockey_or_driver,
        "trainer": runner.trainer,
        "score": view.primary_score,
        "scratched": False,
        "silk": "",
        "why": view.why,
        "key_factors": "Strong recent form; suits the trip.",
        "profile_url": runner.profile_url,
        "race_url": view.race_url,
    }


def load_demo_grids(window) -> None:
    """Populate Race Day / Details / History without fetching."""
    from datetime import date

    from services.result_service import daily_summary

    views = demo_upcoming_views()
    now = NOW
    window.race_day.upcoming_model.set_rows([race_to_row(v, now) for v in views])
    window.race_day.hero.set_view(views[0], now)
    picks = demo_pick_rows()
    window.race_day.picks_model.set_rows(picks)
    summary = daily_summary(picks)
    window.race_day.set_summary(summary)
    window.race_day.empty.setText("")
    window.details.set_view(views[0])
    window.controller.selected_key = views[0].race_key
    window.history.model.set_rows(picks)
    window.history.detail.setPlainText(
        "Locked: True\nPrimary: 3. ALPHA STAR\nBackup: 9. BRAVO\nConfirmed result: Win"
    )
    window.controller.chosen_date = date(2026, 8, 29)
    window.nav.setCurrentRow(0)
