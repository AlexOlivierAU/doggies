"""Table configuration and semantic colour helpers backed by the active theme."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import QAbstractItemView, QHeaderView, QTableView

from desktop.themes.theme_manager import current
from services.confidence import LABEL_CLOSE, LABEL_MEDIUM, LABEL_STRONG
from services.result_service import (
    AWAITING_RESULT,
    BACKUP_PROMOTED,
    BACKUP_WON,
    BOTH_SCRATCHED,
    LOST,
    NO_ACTIVE_SELECTION,
    PENDING,
    PLACED,
    PRIMARY_SCRATCHED,
    RESULT_UNAVAILABLE,
    VOID,
    WIN,
)

ROW_HEIGHT = 32
SILK_LOGICAL = 26
SILK_DIALOG = 110


def result_tone(status: str) -> str:
    mapping = {
        WIN: "win",
        PLACED: "placed",
        LOST: "lost",
        BACKUP_WON: "backup_won",
        PRIMARY_SCRATCHED: "void",
        BACKUP_PROMOTED: "backup_won",
        BOTH_SCRATCHED: "scratch",
        NO_ACTIVE_SELECTION: "scratch",
        VOID: "void",
        RESULT_UNAVAILABLE: "unavailable",
        AWAITING_RESULT: "awaiting",
        PENDING: "pending",
    }
    return mapping.get(str(status or ""), "pending")


def upcoming_tone(*, urgency: str = "", status: str = "") -> str:
    if urgency == "red" or status == "scratching":
        return "scratch"
    if urgency == "amber":
        return "urgent"
    if status == "finished":
        return "finished"
    return ""


def fill_for_tone(tone: str) -> QColor:
    s = current().semantic
    mapping = {
        "urgent": s.urgent_fill,
        "scratch": s.scratch_fill,
        "finished": s.finished_fill,
        "primary": s.pick_primary_fill,
        "backup": s.pick_backup_fill,
        "win": s.win_fill,
        "placed": s.placed_fill,
        "backup_won": s.backup_win_fill,
        "lost": s.loss_fill,
        "awaiting": s.awaiting_fill,
        "unavailable": s.unavailable_fill,
        "void": s.void_fill,
        "pending": s.pending_fill,
    }
    return mapping.get(tone or "", QColor(0, 0, 0, 0))


def accent_for_tone(tone: str) -> QColor:
    s = current().semantic
    mapping = {
        "urgent": s.urgent,
        "scratch": s.scratch,
        "finished": s.finished,
        "primary": s.pick_primary,
        "backup": s.pick_backup,
        "win": s.win,
        "placed": s.win,
        "backup_won": s.backup_win,
        "lost": s.loss,
        "awaiting": s.awaiting,
        "unavailable": s.unavailable,
        "void": s.finished,
    }
    return mapping.get(tone or "", QColor(0, 0, 0, 0))


def badge_colors(label: str) -> tuple[QColor, QColor]:
    s = current().semantic
    c = current().chrome
    mapping = {
        LABEL_STRONG: (s.strong, c.selection_text),
        LABEL_MEDIUM: (s.medium, c.app_bg),
        LABEL_CLOSE: (s.close, c.selection_text),
        WIN: (s.win, c.selection_text),
        PLACED: (s.win, c.selection_text),
        LOST: (s.loss, c.selection_text),
        BACKUP_WON: (s.backup_win, c.selection_text),
        PRIMARY_SCRATCHED: (s.finished, c.selection_text),
        BACKUP_PROMOTED: (s.backup_win, c.selection_text),
        BOTH_SCRATCHED: (s.scratch, c.selection_text),
        NO_ACTIVE_SELECTION: (s.scratch, c.selection_text),
        AWAITING_RESULT: (s.awaiting, c.selection_text),
        PENDING: (s.close, c.selection_text),
        RESULT_UNAVAILABLE: (s.unavailable, c.app_bg),
        VOID: (s.finished, c.selection_text),
    }
    return mapping.get(str(label or ""), (s.close, c.selection_text))


def fluc_color(arrow: str) -> QColor:
    s = current().semantic
    if arrow == "↓":
        return s.shorten
    if arrow == "↑":
        return s.drift
    return s.steady


def class_arrow_color(arrow: str) -> QColor:
    s = current().semantic
    if arrow == "↑":
        return s.class_up
    if arrow == "↓":
        return s.class_down
    return s.steady


def class_arrow_tip(arrow: str) -> str:
    if arrow == "↑":
        return "Stepping up in class"
    if arrow == "↓":
        return "Dropping in class"
    if arrow == "→":
        return "Same or equivalent class"
    return "Class movement unknown"


def fluc_tip(arrow: str) -> str:
    if arrow == "↓":
        return "Shortening (price down)"
    if arrow == "↑":
        return "Drifting (price up)"
    if arrow == "→":
        return "Steady"
    return "No fluctuation"


def configure_table(
    view: QTableView,
    *,
    sorting: bool = True,
    stretch_last: bool = True,
    row_height: int = ROW_HEIGHT,
) -> None:
    view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    view.setAlternatingRowColors(True)
    view.verticalHeader().setVisible(False)
    view.verticalHeader().setDefaultSectionSize(row_height)
    view.horizontalHeader().setStretchLastSection(stretch_last)
    view.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
    view.horizontalHeader().setMinimumSectionSize(36)
    view.setShowGrid(True)
    view.setWordWrap(False)
    view.setTextElideMode(Qt.TextElideMode.ElideRight)
    view.setSortingEnabled(sorting)
    view.setTabKeyNavigation(True)
    view.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
    font = view.font()
    font.setStyleHint(QFont.StyleHint.SansSerif)
    view.setFont(font)
    view.setMouseTracking(True)
