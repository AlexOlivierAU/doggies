from __future__ import annotations

from pathlib import Path

import pytest

from desktop.refresh_gate import RefreshGate, merge_kinds
from race_db import get_pick
from services.pick_service import build_snapshot_payload, save_selection_snapshot
from services.result_service import BACKUP_WON, PRIMARY_SCRATCHED, WIN, resolve_pick_result
from models import Runner
from datetime import date

pytest.importorskip("PySide6")


def test_refresh_coalesce():
    g = RefreshGate()
    assert g.request("odds") is True
    assert g.request("results") is False
    assert g.request("card") is False
    follow = g.finish()
    assert follow == "card"
    assert g.request("odds") is True
    g.finish()


def test_merge_kinds():
    assert merge_kinds("odds", "results") == "all"
    assert merge_kinds("all", "odds") == "all"
    assert merge_kinds(None, "odds") == "odds"


def test_worker_success_and_failure_signals(qapp, tmp_path, monkeypatch):
    from desktop.workers.refresh_worker import RefreshWorker, build_bundle
    from services.card_loader import RefreshPayload

    worker = RefreshWorker()
    got = []
    failed = []
    worker.bundle_ready.connect(got.append)
    worker.failed.connect(lambda k, m: failed.append((k, m)))
    finished = []
    worker.finished_kind.connect(finished.append)

    monkeypatch.setattr(
        "desktop.workers.refresh_worker.build_bundle",
        lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    worker.run("card", {"chosen_date": date(2026, 8, 29), "db_path": str(tmp_path / "x.db")})
    qapp.processEvents()
    assert failed
    assert finished == ["card"]

    def fake_bundle(**kw):
        return type("B", (), {"payload": RefreshPayload("card", "success", "ok"), "views": [], "picks": [], "results": {}, "odds_index": {}, "odds_by_event": {}})()

    monkeypatch.setattr("desktop.workers.refresh_worker.build_bundle", lambda **kw: fake_bundle())
    worker = RefreshWorker()
    got.clear()
    worker.bundle_ready.connect(got.append)
    worker.run("odds", {"chosen_date": date(2026, 8, 29), "db_path": str(tmp_path / "x.db")})
    qapp.processEvents()
    assert got


def test_locked_snapshot_stable(tmp_path: Path):
    db = tmp_path / "roster.db"
    d = date(2026, 8, 29)
    payload = build_snapshot_payload(
        meeting_date=d,
        code="thoroughbred",
        venue="Randwick",
        meeting_url="https://example.test/r",
        race_no=1,
        race_name="BM64",
        race_url="",
        primary="Sarah's Sonnets",
        backup="Dracena",
        primary_score=0.7,
        backup_score=0.6,
        primary_number=5,
        backup_number=4,
        field=[{"name": "Sarah's Sonnets", "program_number": 5, "draw": 12}],
    )
    save_selection_snapshot(
        meeting_date=d,
        meeting_url="https://example.test/r",
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
    blocked = save_selection_snapshot(
        meeting_date=d,
        meeting_url="https://example.test/r",
        code="thoroughbred",
        race_no=1,
        venue="Randwick",
        race_label="R1",
        primary="Someone Else",
        backup="Nope",
        db_path=db,
    )
    assert blocked is False
    stored = get_pick(d, "https://example.test/r", 1, db_path=db)
    assert stored["pick_name"] == "Sarah's Sonnets"
    assert stored["primary_number"] == 5
    assert stored["backup_number"] == 4


def test_result_matching_backup_and_scratch():
    win = resolve_pick_result(
        {"pick_name": "Alpha", "backup": "Beta"},
        {"winner": "Alpha", "place2": "X", "place3": "Y"},
        jumped=True,
    )
    assert win.status == WIN
    scratch = resolve_pick_result(
        {"pick_name": "Alpha", "backup": "Beta", "original_primary": "Alpha", "primary_scratched": True},
        {"winner": "Beta", "place2": "X", "place3": "Y"},
        jumped=True,
    )
    assert scratch.status == BACKUP_WON


def test_settings_persistence(qapp, tmp_path, monkeypatch):
    from PySide6.QtCore import QSettings
    from desktop.settings import DesktopSettings

    ini = str(tmp_path / "settings.ini")
    qs = QSettings(ini, QSettings.Format.IniFormat)
    s = DesktopSettings(qs)
    s.timezone = "Australia/Perth"
    s.state_filter = "WA"
    s.interval_odds_sec = 60
    s.sync()
    s2 = DesktopSettings(QSettings(ini, QSettings.Format.IniFormat))
    assert s2.timezone == "Australia/Perth"
    assert s2.state_filter == "WA"
    assert s2.interval_odds_sec == 60


def test_import_desktop_entry_without_event_loop():
    import desktop.main as mod

    assert hasattr(mod, "create_app")
    assert hasattr(mod, "main")
