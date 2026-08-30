from __future__ import annotations

import os
from pathlib import Path

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings, Qt

from desktop.roles import ROW_TONE_ROLE
from desktop.settings import DesktopSettings
from desktop.themes import THEME_BLUE, THEME_DARK, THEME_IDS
from desktop.themes.theme_manager import (
    THEMES,
    contrast_ratio,
    current,
    migrate_qsettings,
    resolve_from_qsettings,
    set_current,
    theme_complete,
)
from desktop.models.picks_table_model import PicksTableModel
from desktop.demo_fixture import demo_pick_rows, load_demo_grids

DESKTOP = Path(__file__).resolve().parents[2] / "desktop"
CODE_ROOTS = [
    DESKTOP / "models",
    DESKTOP / "delegates",
    DESKTOP / "widgets",
]
FORBIDDEN_HEX = ("#121212", "#161616", "#1a1a1a", "#1e1e1e")


def _ini(tmp_path) -> QSettings:
    return QSettings(str(tmp_path / "theme.ini"), QSettings.Format.IniFormat)


def test_blue_is_default_for_fresh_qsettings(tmp_path):
    s = _ini(tmp_path)
    assert resolve_from_qsettings(s) == THEME_BLUE
    ident = migrate_qsettings(s)
    assert ident == THEME_BLUE
    assert str(s.value("theme")) == THEME_BLUE
    settings = DesktopSettings(s)
    assert settings.theme == THEME_BLUE
    assert settings.theme_explicit is False


def test_legacy_unset_and_dark_default_migrate_to_blue(tmp_path):
    unset = _ini(tmp_path)
    assert migrate_qsettings(unset) == THEME_BLUE

    legacy = QSettings(str(tmp_path / "legacy.ini"), QSettings.Format.IniFormat)
    legacy.setValue("theme", "dark")
    assert resolve_from_qsettings(legacy) == THEME_BLUE
    assert migrate_qsettings(legacy) == THEME_BLUE
    assert str(legacy.value("theme")) == THEME_BLUE
    assert not DesktopSettings(legacy).theme_explicit


def test_explicit_classic_dark_is_preserved(tmp_path):
    s = _ini(tmp_path)
    settings = DesktopSettings(s)
    settings.theme = THEME_DARK
    settings.sync()
    assert settings.theme_explicit is True
    assert resolve_from_qsettings(s) == THEME_DARK
    again = DesktopSettings(QSettings(str(tmp_path / "theme.ini"), QSettings.Format.IniFormat))
    assert again.theme == THEME_DARK


def test_theme_selection_persists(tmp_path):
    path = str(tmp_path / "persist.ini")
    s = QSettings(path, QSettings.Format.IniFormat)
    DesktopSettings(s).theme = THEME_DARK
    s.sync()
    loaded = DesktopSettings(QSettings(path, QSettings.Format.IniFormat))
    assert loaded.theme == THEME_DARK
    loaded.theme = THEME_BLUE
    loaded.sync()
    assert DesktopSettings(QSettings(path, QSettings.Format.IniFormat)).theme == THEME_BLUE


def test_theme_tokens_are_complete(qapp):
    for ident in THEME_IDS:
        missing = theme_complete(THEMES[ident])
        assert missing == [], missing
    theme = set_current(THEME_BLUE)
    assert theme.semantic.win.isValid()
    assert theme.table.alternate_row.isValid()
    assert theme.pick.primary.isValid()
    assert theme.icon.tint.isValid()
    assert theme.chart.series_1.isValid()


def test_models_do_not_hardcode_obsolete_palette():
    hits = []
    for root in CODE_ROOTS:
        for path in root.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if "PALETTE" in text:
                hits.append(f"{path}: PALETTE")
            for hex_colour in FORBIDDEN_HEX:
                if hex_colour.lower() in text.lower():
                    hits.append(f"{path}: {hex_colour}")
    assert hits == []


def test_picks_use_semantic_roles_not_foreground_colours(qapp):
    model = PicksTableModel()
    model.set_rows(demo_pick_rows())
    assert model.data(model.index(0, 0), ROW_TONE_ROLE) == "win"
    assert model.data(model.index(1, 0), ROW_TONE_ROLE) == "backup_won"
    assert model.data(model.index(0, 0), Qt.ItemDataRole.ForegroundRole) is None
    theme = current()
    assert theme.semantic.win.name() != theme.semantic.backup_win.name()
    assert theme.semantic.loss.name() != theme.table.selection.name()
    assert theme.semantic.scratch.name() != theme.table.selection.name()


def test_theme_switch_does_not_clear_rows_or_selection(qapp, tmp_path):
    from desktop.application_controller import ApplicationController
    from desktop.main_window import MainWindow

    settings = DesktopSettings(_ini(tmp_path))
    settings.auto_refresh = False
    settings.db_path = tmp_path / "roster.db"
    window = MainWindow(ApplicationController(settings))
    try:
        load_demo_grids(window)
        qapp.processEvents()
        key = window.race_day.upcoming_model.row_at(1)["race_key"]
        window.race_day.restore_selection(key)
        selected = window.controller.selected_key
        upcoming_n = window.race_day.upcoming_model.rowCount()
        picks_n = window.race_day.picks_model.rowCount()
        details_n = window.details.model.rowCount()
        window._apply_theme(THEME_DARK)
        qapp.processEvents()
        window._apply_theme(THEME_BLUE)
        qapp.processEvents()
        assert window.race_day.upcoming_model.rowCount() == upcoming_n
        assert window.race_day.picks_model.rowCount() == picks_n
        assert window.details.model.rowCount() == details_n
        assert window.race_day.selected_upcoming_key() == key
        assert window.controller.selected_key == selected
        assert current().ident == THEME_BLUE
    finally:
        from desktop.images.silk_cache import reset_silk_cache

        window.controller.shutdown()
        window.close()
        qapp.processEvents()
        reset_silk_cache()


def test_critical_contrast_and_semantic_distinct_from_selection(qapp):
    theme = set_current(THEME_BLUE)
    pairs = [
        (theme.chrome.text, theme.chrome.app_bg),
        (theme.chrome.text, theme.chrome.table_bg),
        (theme.chrome.text, theme.chrome.table_alt),
        (theme.chrome.selection_text, theme.chrome.selection),
        (theme.chrome.secondary, theme.chrome.app_bg),
    ]
    for fg, bg in pairs:
        assert contrast_ratio(fg, bg) >= 3.0, (fg.name(), bg.name(), contrast_ratio(fg, bg))
    assert contrast_ratio(theme.chrome.text, theme.chrome.app_bg) >= 7.0
    selection = theme.table.selection
    for semantic in (theme.semantic.win, theme.semantic.loss, theme.semantic.scratch, theme.semantic.backup_win):
        assert semantic.name().lower() != selection.name().lower()
        dist = (
            (semantic.red() - selection.red()) ** 2
            + (semantic.green() - selection.green()) ** 2
            + (semantic.blue() - selection.blue()) ** 2
        ) ** 0.5
        assert dist >= 80, (semantic.name(), selection.name(), dist)


def test_create_app_applies_blue_without_visiting_settings(qapp, tmp_path, monkeypatch):
    from desktop.main import create_app
    from desktop.themes.theme_manager import current as live

    isolated = DesktopSettings(_ini(tmp_path))

    class IsolatedSettings(DesktopSettings):
        def __init__(self, settings=None) -> None:
            super().__init__(isolated._s)

    monkeypatch.setattr("desktop.settings.DesktopSettings", IsolatedSettings)
    app, window = create_app(["race-day-rater-tests"])
    try:
        assert live().ident == THEME_BLUE
        sheet = app.styleSheet()
        assert "#08111F" in sheet
        assert "#2478C4" in sheet
        labels = [window.settings_page.theme.itemText(i) for i in range(window.settings_page.theme.count())]
        assert "Blue" in labels
        assert "Classic Dark" in labels
        assert window.settings_page.theme.currentData() == THEME_BLUE
    finally:
        window.controller.shutdown()
        window.close()
