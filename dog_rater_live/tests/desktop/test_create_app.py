from __future__ import annotations

import os
from pathlib import Path

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def test_create_app_does_not_require_show(qapp, tmp_path, monkeypatch):
    from PySide6.QtCore import QSettings
    from desktop.main import create_app
    from desktop.settings import DesktopSettings
    from desktop.application_controller import ApplicationController
    from desktop.main_window import MainWindow

    ini = str(tmp_path / "s.ini")
    settings = DesktopSettings(QSettings(ini, QSettings.Format.IniFormat))
    settings.db_path = tmp_path / "roster.db"
    settings.auto_refresh = False
    controller = ApplicationController(settings)
    window = MainWindow(controller)
    try:
        assert window.windowTitle() == "Race Day Rater"
        assert window.stack.currentIndex() == 0
        qapp.processEvents()
    finally:
        controller.shutdown()
        window.close()
