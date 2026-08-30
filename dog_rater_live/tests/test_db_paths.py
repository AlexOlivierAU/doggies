from __future__ import annotations

import pytest

from db_cache import default_db_path
from race_db import default_db_path as race_default


def test_default_db_path_is_absolute_and_shared():
    a = default_db_path()
    b = race_default()
    assert a.is_absolute()
    assert b.is_absolute()
    assert a == b
    assert a.name == "roster.db"
    assert a.parent.name == "cache"


def test_desktop_and_streamlit_share_default(tmp_path, monkeypatch):
    pytest.importorskip("PySide6")
    from PySide6.QtCore import QSettings

    from desktop.paths import shared_default_db_path
    from desktop.settings import DesktopSettings

    ini = str(tmp_path / "s.ini")
    settings = DesktopSettings(QSettings(ini, QSettings.Format.IniFormat))
    assert settings.db_path == shared_default_db_path()
    assert settings.db_path.is_absolute()
    assert settings.db_path == default_db_path()


def test_obsolete_relative_db_path_falls_back(tmp_path, monkeypatch):
    pytest.importorskip("PySide6")
    from PySide6.QtCore import QSettings

    from desktop.settings import DesktopSettings

    ini = str(tmp_path / "s.ini")
    qs = QSettings(ini, QSettings.Format.IniFormat)
    qs.setValue("db_path", "cache/roster.db")
    qs.sync()
    settings = DesktopSettings(QSettings(ini, QSettings.Format.IniFormat))
    assert settings.db_path == default_db_path()
    assert settings.obsolete_db_path == "cache/roster.db"
    assert settings.db_path_warning
    settings.reset_db_path()
    assert settings.stored_db_path == ""
    assert settings.db_path == default_db_path()


def test_missing_custom_parent_does_not_silently_create(tmp_path):
    pytest.importorskip("PySide6")
    from PySide6.QtCore import QSettings

    from desktop.settings import DesktopSettings

    ini = str(tmp_path / "s.ini")
    qs = QSettings(ini, QSettings.Format.IniFormat)
    ghost = tmp_path / "missing-dir" / "ghost.db"
    qs.setValue("db_path", str(ghost))
    qs.sync()
    settings = DesktopSettings(QSettings(ini, QSettings.Format.IniFormat))
    assert settings.db_path == default_db_path()
    assert str(ghost) in settings.obsolete_db_path
    assert not ghost.exists()
