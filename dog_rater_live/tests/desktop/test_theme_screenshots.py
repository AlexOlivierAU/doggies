from __future__ import annotations

import os
from pathlib import Path

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings

from desktop.demo_fixture import demo_runner_detail, load_demo_grids
from desktop.settings import DesktopSettings
from desktop.themes import THEME_BLUE
from desktop.themes.theme_manager import apply_to_application, set_current
from desktop.widgets.runner_details_dialog import RunnerDetailsDialog

SCREEN_DIR = Path(__file__).resolve().parents[2] / "desktop" / "resources" / "screenshots"


def _window(qapp, tmp_path):
    from desktop.application_controller import ApplicationController
    from desktop.main_window import MainWindow

    s = QSettings(str(tmp_path / "s.ini"), QSettings.Format.IniFormat)
    settings = DesktopSettings(s)
    settings.auto_refresh = False
    settings.db_path = tmp_path / "roster.db"
    settings.theme = THEME_BLUE
    set_current(THEME_BLUE)
    apply_to_application(qapp, THEME_BLUE)
    window = MainWindow(ApplicationController(settings))
    load_demo_grids(window)
    window.resize(1280, 800)
    window.show()
    qapp.processEvents()
    return window


def _save(widget, name: str) -> Path:
    SCREEN_DIR.mkdir(parents=True, exist_ok=True)
    path = SCREEN_DIR / name
    pix = widget.grab()
    assert pix.width() > 200
    assert pix.height() > 120
    assert pix.save(str(path), "PNG")
    assert path.exists() and path.stat().st_size > 1000
    return path


def test_capture_blue_screens(qapp, tmp_path):
    window = _window(qapp, tmp_path)
    try:
        window.nav.setCurrentRow(0)
        qapp.processEvents()
        _save(window, "blue-race-day.png")

        window.nav.setCurrentRow(1)
        qapp.processEvents()
        _save(window, "blue-race-details.png")

        window.nav.setCurrentRow(2)
        qapp.processEvents()
        from desktop.demo_fixture import demo_pick_rows

        if window.history.model.rowCount():
            window.history.table.selectRow(0)
        qapp.processEvents()
        window.history.detail.setPlainText(
            "Locked: True\nPrimary: 3. ALPHA STAR\nBackup: 9. BRAVO\nConfirmed result: Win"
        )
        qapp.processEvents()
        _save(window, "blue-history.png")

        dlg = RunnerDetailsDialog(demo_runner_detail(), window)
        dlg.resize(440, 560)
        dlg.show()
        qapp.processEvents()
        _save(dlg, "blue-runner-details.png")
        dlg.close()
    finally:
        from desktop.images.silk_cache import reset_silk_cache

        window.controller.shutdown()
        window.close()
        qapp.processEvents()
        reset_silk_cache()
