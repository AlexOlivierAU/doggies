"""Today's picks / results table model."""

from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QColor

from services.formatting import format_saved_selection
from services.result_service import (
    BACKUP_WON,
    LOST,
    PRIMARY_SCRATCHED,
    RESULT_UNAVAILABLE,
    VOID,
    WIN,
    daily_summary,
    resolve_pick_result,
)

HEADERS = [
    "Result",
    "Jump",
    "Venue",
    "Race",
    "Primary",
    "Primary finish",
    "Saved odds",
    "Backup",
    "Backup finish",
    "Confidence",
    "Source",
]

STATUS_COLOURS = {
    WIN: QColor("#2e7d32"),
    BACKUP_WON: QColor("#6a1b9a"),
    LOST: QColor("#c62828"),
    PRIMARY_SCRATCHED: QColor("#9e9e9e"),
    VOID: QColor("#9e9e9e"),
    RESULT_UNAVAILABLE: QColor("#546e7a"),
}


def pick_rows_from_views(views, picks_index: dict, results_by_key: dict, now) -> tuple[list[dict[str, Any]], Any]:
    view_by_key = {v.race_key: v for v in views or []}
    table: list[dict[str, Any]] = []
    resolved_rows = []
    for key, pick in sorted(
        (picks_index or {}).items(),
        key=lambda kv: (kv[1].get("scheduled_jump") or "", kv[1].get("venue") or "", kv[0][1]),
    ):
        view = view_by_key.get(key)
        jump_at = view.jump_at if view else None
        result = results_by_key.get(key) or results_by_key.get((key[0], int(key[1]))) or {}
        resolved = resolve_pick_result(pick, result, now=now, jump_at=jump_at)
        row = {
            **pick,
            **resolved.as_dict(),
            "result": resolved.status,
            "jump": view.clock() if view else (str(pick.get("scheduled_jump") or "")[11:16] or "—"),
            "venue": pick.get("venue") or (view.venue if view else ""),
            "race": f"R{pick.get('race_no')}",
            "race_no": pick.get("race_no"),
            "meeting_url": pick.get("meeting_url"),
            "primary": format_saved_selection(pick, "primary"),
            "backup": format_saved_selection(pick, "backup"),
            "primary_finish": resolved.primary_finish_label,
            "backup_finish": resolved.backup_finish_label,
            "saved_odds": pick.get("primary_odds"),
            "confidence": pick.get("confidence_label") or (view.confidence_label if view else ""),
            "source": resolved.result_source or resolved.match_note,
            "race_key": key,
            "jump_at": jump_at,
        }
        resolved_rows.append(row)
        table.append(row)
    summary = daily_summary(resolved_rows)
    return table, summary


class PicksTableModel(QAbstractTableModel):
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
        if role == Qt.ItemDataRole.DisplayRole:
            odds = row.get("saved_odds")
            odds_txt = f"${float(odds):.2f}" if isinstance(odds, (int, float)) else "—"
            values = [
                row.get("result") or "",
                row.get("jump") or "—",
                row.get("venue") or "",
                row.get("race") or "",
                row.get("primary") or "",
                row.get("primary_finish") or "—",
                odds_txt,
                row.get("backup") or "",
                row.get("backup_finish") or "—",
                row.get("confidence") or "—",
                row.get("source") or "",
            ]
            return values[col]
        if role == Qt.ItemDataRole.ForegroundRole:
            colour = STATUS_COLOURS.get(str(row.get("result") or ""))
            if colour is not None and col == 0:
                return colour
        if role == Qt.ItemDataRole.UserRole:
            return row.get("race_key")
        return None

    def flags(self, index: QModelIndex):
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    def set_rows(self, rows: list[dict[str, Any]]) -> None:
        self.beginResetModel()
        self._rows = list(rows)
        self.endResetModel()

    def row_at(self, row: int) -> Optional[dict[str, Any]]:
        if 0 <= row < len(self._rows):
            return self._rows[row]
        return None

    @property
    def rows(self) -> list[dict[str, Any]]:
        return list(self._rows)
