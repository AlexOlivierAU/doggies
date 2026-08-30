"""Today's picks / results table model."""

from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt

from desktop.roles import (
    BARRIER_ROLE,
    CONFIDENCE_ROLE,
    DETAIL_ROLE,
    HORSE_NAME_ROLE,
    ODDS_ROLE,
    PICK_ROLE,
    PROGRAM_NUMBER_ROLE,
    RACE_KEY_ROLE,
    RESULT_STATUS_ROLE,
    ROW_TONE_ROLE,
    SCRATCHED_ROLE,
    SILK_URL_ROLE,
    SORT_ROLE,
    SOURCE_ROLE,
)
from desktop.table_theme import result_tone
from services.formatting import format_saved_selection
from services.result_service import daily_summary, resolve_pick_result
from services.runner_numbers import saved_pick_number

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


def _field_entry(pick: dict, name: str) -> dict:
    snap = pick.get("snapshot") if isinstance(pick.get("snapshot"), dict) else {}
    field = snap.get("field") or pick.get("field") or []
    for item in field or []:
        if str(item.get("name") or "") == str(name or ""):
            return item
    return {}


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
        primary_name = str(pick.get("original_primary") or pick.get("pick_name") or "")
        backup_name = str(pick.get("backup") or "")
        p_field = _field_entry(pick, primary_name)
        b_field = _field_entry(pick, backup_name)
        status = resolved.status
        row = {
            **pick,
            **resolved.as_dict(),
            "result": status,
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
            "row_tone": result_tone(status),
            "primary_name": primary_name,
            "backup_name": backup_name,
            "primary_no": saved_pick_number(pick, "primary"),
            "backup_no": saved_pick_number(pick, "backup"),
            "primary_silk": p_field.get("silk_url") or "",
            "backup_silk": b_field.get("silk_url") or "",
            "primary_barrier": p_field.get("draw"),
            "backup_barrier": b_field.get("draw"),
            "primary_scratched": bool(p_field.get("scratched") or pick.get("primary_scratched")),
            "backup_scratched": bool(b_field.get("scratched") or pick.get("backup_scratched")),
            "primary_odds": pick.get("primary_odds"),
            "backup_odds": pick.get("backup_odds"),
            "from_snapshot": True,
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
        if role == Qt.ItemDataRole.ToolTipRole:
            if col == 10:
                return str(row.get("source") or "")
            if col == 0:
                return str(row.get("result") or "")
            return self.data(index, Qt.ItemDataRole.DisplayRole)
        if role == RACE_KEY_ROLE or role == Qt.ItemDataRole.UserRole:
            return row.get("race_key")
        if role == ROW_TONE_ROLE:
            return row.get("row_tone")
        if role == RESULT_STATUS_ROLE and col == 0:
            return row.get("result")
        if role == CONFIDENCE_ROLE and col == 9:
            return row.get("confidence")
        if role == SOURCE_ROLE:
            return row.get("source")
        if col == 4:
            return _saved_pick_role(row, "primary", role)
        if col == 7:
            return _saved_pick_role(row, "backup", role)
        if col == 6:
            if role == ODDS_ROLE:
                return row.get("saved_odds")
            if role == SORT_ROLE:
                odds = row.get("saved_odds")
                return float(odds) if isinstance(odds, (int, float)) else -1.0
        if role == DETAIL_ROLE:
            return row
        if role == SORT_ROLE and col == 0:
            return str(row.get("result") or "")
        return None

    def flags(self, index: QModelIndex):
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    def set_rows(self, rows: list[dict[str, Any]]) -> None:
        self.beginResetModel()
        self._rows = list(rows)
        self.endResetModel()

    def silk_urls(self) -> list[str]:
        out = []
        for row in self._rows:
            for key in ("primary_silk", "backup_silk"):
                if row.get(key):
                    out.append(row[key])
        return out

    def indexes_for_silk(self, url: str) -> list[QModelIndex]:
        found = []
        for i, row in enumerate(self._rows):
            if row.get("primary_silk") == url:
                found.append(self.index(i, 4))
            if row.get("backup_silk") == url:
                found.append(self.index(i, 7))
        return found

    def find_row(self, race_key) -> int:
        for i, row in enumerate(self._rows):
            if row.get("race_key") == race_key:
                return i
        return -1

    def row_at(self, row: int) -> Optional[dict[str, Any]]:
        if 0 <= row < len(self._rows):
            return self._rows[row]
        return None

    @property
    def rows(self) -> list[dict[str, Any]]:
        return list(self._rows)


def _saved_pick_role(row: dict, which: str, role):
    prefix = "primary" if which == "primary" else "backup"
    if role == SILK_URL_ROLE:
        return row.get(f"{prefix}_silk") or ""
    if role == PROGRAM_NUMBER_ROLE:
        return row.get(f"{prefix}_no")
    if role == HORSE_NAME_ROLE:
        return row.get(f"{prefix}_name")
    if role == BARRIER_ROLE:
        return row.get(f"{prefix}_barrier")
    if role == ODDS_ROLE:
        return row.get(f"{prefix}_odds") if which == "primary" else row.get("backup_odds")
    if role == PICK_ROLE:
        return which
    if role == SCRATCHED_ROLE:
        return row.get(f"{prefix}_scratched")
    return None
