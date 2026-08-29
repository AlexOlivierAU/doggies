"""QAbstractTableModel for the upcoming-races table."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QColor

from services.formatting import format_clock, format_countdown, format_runner_pick
from services.race_day_service import RaceView, urgency_color

HEADERS = [
    "Jump",
    "Countdown",
    "Venue",
    "Race",
    "Primary",
    "Odds",
    "Backup",
    "Confidence",
    "Status",
]

RACE_KEY_ROLE = Qt.ItemDataRole.UserRole
URGENCY_ROLE = Qt.ItemDataRole.UserRole + 1

COLOURS = {
    "amber": QColor("#ef6c00"),
    "red": QColor("#c62828"),
    "grey": QColor("#9e9e9e"),
    "green": QColor("#2e7d32"),
    "purple": QColor("#6a1b9a"),
}


def race_to_row(view: RaceView, now: datetime) -> dict[str, Any]:
    odds_txt = "—"
    if view.odds is not None:
        try:
            odds_txt = f"${float(view.odds):.2f}"
        except (TypeError, ValueError):
            odds_txt = "—"
    return {
        "jump": format_clock(view.jump_at),
        "countdown": format_countdown(view.jump_at, now),
        "venue": view.venue,
        "race": f"R{view.race_no}",
        "primary": format_runner_pick(view.primary_no, view.primary),
        "odds": odds_txt,
        "odds_value": view.odds,
        "backup": format_runner_pick(view.backup_no, view.backup, view.backup_odds),
        "confidence": view.confidence_label,
        "status": view.status,
        "jump_at": view.jump_at,
        "race_key": view.race_key,
        "urgency": urgency_color(view, now),
        "meeting_url": view.meeting_url,
        "race_no": view.race_no,
        "primary_no": view.primary_no,
        "backup_no": view.backup_no,
        "barrier_note": "",
    }


class RaceTableModel(QAbstractTableModel):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._rows: list[dict[str, Any]] = []

    def rowCount(self, parent=QModelIndex()) -> int:
        if parent.isValid():
            return 0
        return len(self._rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        if parent.isValid():
            return 0
        return len(HEADERS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            if 0 <= section < len(HEADERS):
                return HEADERS[section]
        return None

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not (0 <= index.row() < len(self._rows)):
            return None
        row = self._rows[index.row()]
        col = index.column()
        keys = ["jump", "countdown", "venue", "race", "primary", "odds", "backup", "confidence", "status"]
        if role == Qt.ItemDataRole.DisplayRole:
            return row.get(keys[col], "")
        if role == Qt.ItemDataRole.ForegroundRole:
            token = row.get("urgency") or ""
            if token in COLOURS:
                return COLOURS[token]
        if role == RACE_KEY_ROLE:
            return row.get("race_key")
        if role == URGENCY_ROLE:
            return row.get("urgency")
        if role == Qt.ItemDataRole.TextAlignmentRole and col in {1, 3, 5, 8}:
            return int(Qt.AlignmentFlag.AlignCenter)
        return None

    def flags(self, index: QModelIndex):
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    def set_rows(self, rows: list[dict[str, Any]]) -> None:
        self.beginResetModel()
        self._rows = list(rows)
        self.endResetModel()

    def update_countdowns(self, now: datetime) -> None:
        if not self._rows:
            return
        for row in self._rows:
            row["countdown"] = format_countdown(row.get("jump_at"), now)
        top = self.index(0, 1)
        bottom = self.index(len(self._rows) - 1, 1)
        self.dataChanged.emit(top, bottom, [Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ForegroundRole])

    def row_at(self, row: int) -> Optional[dict[str, Any]]:
        if 0 <= row < len(self._rows):
            return self._rows[row]
        return None

    def find_row(self, race_key) -> int:
        for i, row in enumerate(self._rows):
            if row.get("race_key") == race_key:
                return i
        return -1

    @property
    def rows(self) -> list[dict[str, Any]]:
        return list(self._rows)
