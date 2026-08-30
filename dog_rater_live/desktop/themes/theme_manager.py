"""Active desktop theme. Models and delegates import this module only — never the reverse."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from PySide6.QtGui import QColor, QPalette

from desktop.themes import (
    LEGACY_DEFAULTS,
    THEME_BLUE,
    THEME_DARK,
    THEME_IDS,
    label_for,
    normalize_theme_id,
)
from desktop.themes import blue as blue_mod
from desktop.themes import dark as dark_mod

REQUIRED_CHROME = (
    "app_bg",
    "nav_bg",
    "panel",
    "raised",
    "table_bg",
    "table_alt",
    "header",
    "border",
    "hover",
    "selection",
    "selection_inactive",
    "selection_text",
    "primary",
    "primary_hover",
    "text",
    "secondary",
    "disabled",
    "button_bg",
    "input_bg",
    "tooltip_bg",
    "scrollbar",
    "placeholder",
)

REQUIRED_SEMANTIC = (
    "win",
    "win_fill",
    "placed_fill",
    "backup_win",
    "backup_win_fill",
    "loss",
    "loss_fill",
    "scratch",
    "scratch_fill",
    "urgent",
    "urgent_fill",
    "awaiting",
    "awaiting_fill",
    "finished",
    "finished_fill",
    "unavailable",
    "unavailable_fill",
    "void_fill",
    "pending_fill",
    "strong",
    "medium",
    "close",
    "shorten",
    "drift",
    "steady",
    "class_up",
    "class_down",
    "pick_primary",
    "pick_primary_fill",
    "pick_backup",
    "pick_backup_fill",
)


class _Group:
    def __init__(self, raw: dict[str, str]) -> None:
        self._raw = dict(raw)
        self._colours = {k: _qcolor(v) for k, v in raw.items()}

    def __getattr__(self, name: str) -> QColor:
        try:
            return self._colours[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def hex(self, name: str) -> str:
        return self._raw[name]

    def names(self) -> tuple[str, ...]:
        return tuple(self._raw.keys())


def _qcolor(value: str) -> QColor:
    if len(value) == 9 and value.startswith("#"):
        c = QColor(value[:7])
        c.setAlpha(int(value[7:9], 16))
        return c
    return QColor(value)


@dataclass
class Theme:
    ident: str
    label: str
    chrome: _Group
    semantic: _Group
    table: _Group
    pick: _Group
    icon: _Group
    chart: _Group

    def qss(self) -> str:
        return render_qss(self)


def _build(ident: str, label: str, chrome: dict, semantic: dict) -> Theme:
    table = {
        "background": chrome["table_bg"],
        "alternate_row": chrome["table_alt"],
        "header": chrome["header"],
        "border": chrome["border"],
        "selection": chrome["selection"],
        "selection_inactive": chrome.get("selection_inactive", chrome["hover"]),
        "selection_text": chrome["selection_text"],
        "hover": chrome["hover"],
        "text": chrome["text"],
        "muted": chrome["secondary"],
        "disabled": chrome["disabled"],
    }
    pick = {
        "primary": semantic["pick_primary"],
        "primary_fill": semantic["pick_primary_fill"],
        "backup": semantic["pick_backup"],
        "backup_fill": semantic["pick_backup_fill"],
    }
    icon = {
        "tint": chrome["secondary"],
        "active": chrome["text"],
        "accent": chrome["primary"],
        "disabled": chrome["disabled"],
    }
    chart = {
        "series_1": chrome["primary"],
        "series_2": semantic["win"],
        "series_3": semantic["backup_win"],
        "series_4": semantic["urgent"],
        "axis": chrome["secondary"],
        "grid": chrome["border"],
        "background": chrome["panel"],
    }
    return Theme(
        ident=ident,
        label=label,
        chrome=_Group(chrome),
        semantic=_Group(semantic),
        table=_Group(table),
        pick=_Group(pick),
        icon=_Group(icon),
        chart=_Group(chart),
    )


THEMES: dict[str, Theme] = {
    THEME_BLUE: _build(THEME_BLUE, label_for(THEME_BLUE), blue_mod.CHROME, blue_mod.SEMANTIC),
    THEME_DARK: _build(THEME_DARK, label_for(THEME_DARK), dark_mod.CHROME, dark_mod.SEMANTIC),
}

_active: Theme = THEMES[THEME_BLUE]


def current() -> Theme:
    return _active


def set_current(ident: str) -> Theme:
    global _active
    _active = THEMES.get(normalize_theme_id(ident), THEMES[THEME_BLUE])
    return _active


REQUIRED_TABLE = (
    "background",
    "alternate_row",
    "header",
    "border",
    "selection",
    "selection_inactive",
    "selection_text",
    "hover",
    "text",
    "muted",
    "disabled",
)
REQUIRED_PICK = ("primary", "primary_fill", "backup", "backup_fill")
REQUIRED_ICON = ("tint", "active", "accent", "disabled")
REQUIRED_CHART = ("series_1", "series_2", "series_3", "series_4", "axis", "grid", "background")


def theme_complete(theme: Theme) -> list[str]:
    missing = []
    for key in REQUIRED_CHROME:
        if key not in theme.chrome.names():
            missing.append(f"chrome.{key}")
    for key in REQUIRED_SEMANTIC:
        if key not in theme.semantic.names():
            missing.append(f"semantic.{key}")
    for key in REQUIRED_TABLE:
        if key not in theme.table.names():
            missing.append(f"table.{key}")
    for key in REQUIRED_PICK:
        if key not in theme.pick.names():
            missing.append(f"pick.{key}")
    for key in REQUIRED_ICON:
        if key not in theme.icon.names():
            missing.append(f"icon.{key}")
    for key in REQUIRED_CHART:
        if key not in theme.chart.names():
            missing.append(f"chart.{key}")
    return missing


def resolve_from_qsettings(qsettings) -> str:
    """Blue is default. Legacy unset/`dark` without an explicit user choice migrates to Blue."""
    explicit = _truthy(qsettings.value("theme_explicit", False))
    has_theme = qsettings.contains("theme")
    raw = str(qsettings.value("theme", "") or "").strip()
    if explicit:
        ident = normalize_theme_id(raw)
        return ident if ident in THEME_IDS else THEME_BLUE
    if not has_theme or raw.lower() in LEGACY_DEFAULTS:
        return THEME_BLUE
    ident = normalize_theme_id(raw)
    return ident if ident in THEME_IDS else THEME_BLUE


def migrate_qsettings(qsettings) -> str:
    ident = resolve_from_qsettings(qsettings)
    if not qsettings.contains("theme") or (
        not _truthy(qsettings.value("theme_explicit", False))
        and str(qsettings.value("theme", "") or "").strip().lower() in LEGACY_DEFAULTS
    ):
        qsettings.setValue("theme", ident)
    return ident


def _truthy(val: Any) -> bool:
    if isinstance(val, bool):
        return val
    return str(val).lower() in {"1", "true", "yes"}


def _relative_luminance(colour: QColor) -> float:
    def channel(value: int) -> float:
        x = value / 255.0
        return x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4

    return 0.2126 * channel(colour.red()) + 0.7152 * channel(colour.green()) + 0.0722 * channel(colour.blue())


def contrast_ratio(a: QColor, b: QColor) -> float:
    lighter = max(_relative_luminance(a), _relative_luminance(b))
    darker = min(_relative_luminance(a), _relative_luminance(b))
    return (lighter + 0.05) / (darker + 0.05)


def apply_to_application(app, ident: str | None = None) -> Theme:
    """Set Fusion + palette + generated QSS. Does not touch table models or race data."""
    theme = set_current(ident) if ident is not None else current()
    try:
        app.setStyle("Fusion")
    except Exception:
        pass
    c = theme.chrome
    pal = QPalette()
    pal.setColor(QPalette.ColorRole.Window, c.app_bg)
    pal.setColor(QPalette.ColorRole.WindowText, c.text)
    pal.setColor(QPalette.ColorRole.Base, c.table_bg)
    pal.setColor(QPalette.ColorRole.AlternateBase, c.table_alt)
    pal.setColor(QPalette.ColorRole.Text, c.text)
    pal.setColor(QPalette.ColorRole.Button, c.button_bg)
    pal.setColor(QPalette.ColorRole.ButtonText, c.text)
    pal.setColor(QPalette.ColorRole.Highlight, c.selection)
    pal.setColor(QPalette.ColorRole.HighlightedText, c.selection_text)
    pal.setColor(QPalette.ColorRole.ToolTipBase, c.tooltip_bg)
    pal.setColor(QPalette.ColorRole.ToolTipText, c.text)
    pal.setColor(QPalette.ColorRole.PlaceholderText, c.disabled)
    pal.setColor(QPalette.ColorRole.Link, c.primary)
    pal.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, c.disabled)
    pal.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText, c.disabled)
    pal.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, c.disabled)
    pal.setColor(QPalette.ColorGroup.Inactive, QPalette.ColorRole.Highlight, c.selection_inactive)
    pal.setColor(QPalette.ColorGroup.Inactive, QPalette.ColorRole.HighlightedText, c.selection_text)
    app.setPalette(pal)
    app.setStyleSheet(theme.qss())
    return theme


def refresh_styled_widgets(root) -> None:
    """Repaint chrome after a live theme change without resetting models."""
    from PySide6.QtWidgets import QHeaderView, QTableView

    style = root.style()
    style.unpolish(root)
    style.polish(root)
    for table in root.findChildren(QTableView):
        table.viewport().update()
        header = table.horizontalHeader()
        if isinstance(header, QHeaderView):
            header.viewport().update()
    root.update()


def render_qss(theme: Theme) -> str:
    c = theme.chrome
    h = c.hex
    return f"""
QMainWindow, QDialog, QWidget {{
  background: {h("app_bg")};
  color: {h("text")};
  font-size: 13px;
}}
QWidget {{
  font-family: "IBM Plex Sans", "Segoe UI", "Helvetica Neue", sans-serif;
}}
QListWidget {{
  background: {h("nav_bg")};
  border: none;
  border-right: 1px solid {h("border")};
  padding: 8px 0;
  outline: none;
  color: {h("secondary")};
}}
QListWidget::item {{
  padding: 10px 16px;
  color: {h("secondary")};
}}
QListWidget::item:hover {{
  background: {h("hover")};
  color: {h("text")};
}}
QListWidget::item:selected {{
  background: {h("selection")};
  color: {h("selection_text")};
  font-weight: 600;
}}
QToolBar {{
  background: {h("nav_bg")};
  border-bottom: 1px solid {h("border")};
  spacing: 8px;
  padding: 4px 8px;
  color: {h("text")};
}}
QStatusBar {{
  background: {h("nav_bg")};
  color: {h("secondary")};
  border-top: 1px solid {h("border")};
}}
QLabel#kicker {{
  color: {h("secondary")};
  font-size: 11px;
  letter-spacing: 0.06em;
}}
QLabel#heroTitle {{
  font-size: 22px;
  font-weight: 700;
  color: {h("text")};
}}
QLabel#pickLine {{
  font-size: 16px;
  font-weight: 650;
  color: {h("text")};
}}
QFrame#heroCard, QFrame#statCard, QFrame#panel {{
  background: {h("raised")};
  border: 1px solid {h("border")};
  padding: 8px;
}}
QTableView {{
  background: {h("table_bg")};
  alternate-background-color: {h("table_alt")};
  gridline-color: {h("border")};
  selection-background-color: {h("selection")};
  selection-color: {h("selection_text")};
  border: 1px solid {h("border")};
  color: {h("text")};
}}
QTableView::item:hover {{
  background: {h("hover")};
}}
QHeaderView::section {{
  background: {h("header")};
  color: {h("secondary")};
  padding: 6px 8px;
  border: none;
  border-right: 1px solid {h("border")};
  border-bottom: 1px solid {h("border")};
  font-weight: 600;
}}
QPushButton {{
  background: {h("button_bg")};
  color: {h("text")};
  border: 1px solid {h("border")};
  padding: 6px 12px;
  min-height: 24px;
}}
QPushButton:hover {{
  background: {h("hover")};
}}
QPushButton:disabled {{
  color: {h("disabled")};
}}
QPushButton#primaryButton {{
  background: {h("primary")};
  border-color: {h("primary")};
  color: {h("selection_text")};
}}
QPushButton#primaryButton:hover {{
  background: {h("primary_hover")};
}}
QComboBox, QDateEdit, QSpinBox, QLineEdit, QCheckBox, QAbstractSpinBox {{
  background: {h("input_bg")};
  color: {h("text")};
  border: 1px solid {h("border")};
  padding: 4px 6px;
  min-height: 22px;
}}
QComboBox QAbstractItemView {{
  background: {h("raised")};
  color: {h("text")};
  selection-background-color: {h("selection")};
  border: 1px solid {h("border")};
}}
QTextEdit, QPlainTextEdit {{
  background: {h("table_bg")};
  color: {h("text")};
  border: 1px solid {h("border")};
}}
QMenu {{
  background: {h("raised")};
  color: {h("text")};
  border: 1px solid {h("border")};
}}
QMenu::item:selected {{
  background: {h("selection")};
  color: {h("selection_text")};
}}
QMenu::item:disabled {{
  color: {h("disabled")};
}}
QToolTip {{
  background: {h("tooltip_bg")};
  color: {h("text")};
  border: 1px solid {h("border")};
  padding: 4px 6px;
}}
QScrollBar:vertical, QScrollBar:horizontal {{
  background: {h("nav_bg")};
  border: none;
  width: 10px;
  height: 10px;
}}
QScrollBar::handle:vertical, QScrollBar::handle:horizontal {{
  background: {h("scrollbar")};
  min-height: 24px;
  min-width: 24px;
  border-radius: 4px;
}}
QScrollBar::add-line, QScrollBar::sub-line {{
  width: 0;
  height: 0;
}}
QScrollArea {{
  border: none;
  background: {h("app_bg")};
}}
QSplitter::handle {{
  background: {h("border")};
}}
QLabel#healthOk {{ color: {theme.semantic.hex("win")}; font-weight: 650; }}
QLabel#healthWarn {{ color: {theme.semantic.hex("urgent")}; font-weight: 650; }}
QLabel#healthErr {{ color: {theme.semantic.hex("scratch")}; font-weight: 650; }}
QLabel#muted {{ color: {h("secondary")}; }}
QLabel#warn {{ color: {theme.semantic.hex("urgent")}; font-weight: 650; }}
QTableView::item:selected {{
  background: {h("selection")};
  color: {h("selection_text")};
}}
QTableView::item:selected:!active {{
  background: {h("selection_inactive")};
  color: {h("selection_text")};
}}
QTableView::item:disabled {{
  color: {h("disabled")};
}}
QHeaderView {{
  background: {h("header")};
}}
QAbstractScrollArea {{
  background: {h("table_bg")};
  color: {h("text")};
}}
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QDateEdit:disabled, QPushButton:disabled {{
  color: {h("disabled")};
}}
QCheckBox {{
  spacing: 8px;
}}
QCheckBox::indicator {{
  width: 14px;
  height: 14px;
  border: 1px solid {h("border")};
  background: {h("input_bg")};
}}
QCheckBox::indicator:checked {{
  background: {h("primary")};
  border-color: {h("primary")};
}}
QCalendarWidget QWidget {{
  background: {h("raised")};
  color: {h("text")};
}}
QCalendarWidget QAbstractItemView {{
  background: {h("table_bg")};
  color: {h("text")};
  selection-background-color: {h("selection")};
  selection-color: {h("selection_text")};
}}
QCalendarWidget QToolButton {{
  background: {h("button_bg")};
  color: {h("text")};
}}
QMessageBox, QInputDialog, QFileDialog {{
  background: {h("app_bg")};
  color: {h("text")};
}}
QDialogButtonBox QPushButton {{
  min-width: 72px;
}}
QToolBar QLabel {{
  color: {h("secondary")};
}}
QSplitter {{
  background: {h("app_bg")};
}}
"""
