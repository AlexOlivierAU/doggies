"""Ranked field table for Race Details."""

from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt

from parse_racingaustralia import runner_class_arrow, runner_last_class
from services.formatting import format_runner_pick
from services.runner_numbers import program_number_for_runner

HEADERS = [
    "No",
    "Barrier",
    "Name",
    "Odds",
    "Form",
    "Class",
    "Weight",
    "Jockey",
    "Trainer",
    "Score",
    "Status",
]


def details_rows(view, ranked, odds_lookup=None) -> list[dict[str, Any]]:
    runner_by = {getattr(r, "name", ""): r for r in (getattr(view, "runners", None) or [])}
    rows = []
    for rr in ranked or []:
        runner = runner_by.get(rr.name)
        no = program_number_for_runner(runner) if runner is not None else None
        mark = ""
        if rr.name == view.primary:
            mark = "PRIMARY"
        elif rr.name == view.backup:
            mark = "BACKUP"
        odds_txt = "—"
        if odds_lookup:
            o = odds_lookup(view.venue_raw or view.venue, view.race_no, rr.name)
            if o and o.get("win") is not None:
                try:
                    odds_txt = f"${float(o['win']):.2f}"
                except (TypeError, ValueError):
                    odds_txt = "—"
        elif view.primary == rr.name and view.odds is not None:
            odds_txt = f"${float(view.odds):.2f}"
        scratched = bool(getattr(runner, "scratched", False)) if runner else False
        rows.append(
            {
                "no": "" if no is None else str(no),
                "barrier": "" if runner is None or runner.draw is None else str(runner.draw),
                "name": format_runner_pick(no, rr.name),
                "raw_name": rr.name,
                "odds": odds_txt,
                "form": getattr(runner, "last10", None) or "—",
                "class": f"{runner_last_class(runner) if runner else ''} {runner_class_arrow(runner, view.race_class) if runner else ''}".strip()
                or "—",
                "weight": "—" if runner is None or runner.weight_kg is None else str(runner.weight_kg),
                "jockey": (getattr(runner, "jockey_or_driver", None) or "—") if runner else "—",
                "trainer": (getattr(runner, "trainer", None) or "—") if runner else "—",
                "score": f"{rr.score:.3f}",
                "status": "SCRATCHED" if scratched else (mark or "—"),
                "why": list(getattr(rr, "why_bullets", []) or []),
                "debug": getattr(rr, "debug", None),
                "key_factors": getattr(rr, "key_factors", "") or "",
            }
        )
    return rows


class DetailsTableModel(QAbstractTableModel):
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
        if role == Qt.ItemDataRole.DisplayRole:
            keys = [
                "no",
                "barrier",
                "name",
                "odds",
                "form",
                "class",
                "weight",
                "jockey",
                "trainer",
                "score",
                "status",
            ]
            return row.get(keys[index.column()], "")
        if role == Qt.ItemDataRole.UserRole:
            return row
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
