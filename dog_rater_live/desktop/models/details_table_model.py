"""Ranked field table for Race Details."""

from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt

from desktop.roles import (
    BARRIER_ROLE,
    CLASS_ARROW_ROLE,
    CLASS_LABEL_ROLE,
    DETAIL_ROLE,
    FLUCTUATION_HISTORY_ROLE,
    FLUCTUATION_ROLE,
    FORM_ROLE,
    HORSE_NAME_ROLE,
    JOCKEY_ROLE,
    LAST_CLASS_ROLE,
    ODDS_ROLE,
    PICK_ROLE,
    PROGRAM_NUMBER_ROLE,
    PROFILE_URL_ROLE,
    RESULT_STATUS_ROLE,
    ROW_TONE_ROLE,
    SCORE_ROLE,
    SCRATCHED_ROLE,
    SILK_URL_ROLE,
    SORT_ROLE,
    TRAINER_ROLE,
    WEIGHT_ROLE,
    WHY_ROLE,
)
from parse_racingaustralia import runner_class_arrow, runner_last_class
from services.formatting import format_runner_pick
from services.names import names_match
from services.runner_numbers import program_number_for_runner

HEADERS = [
    "Silk",
    "No.",
    "Horse",
    "Barrier",
    "Odds",
    "Fluc",
    "Form",
    "Class",
    "Weight",
    "Jockey",
    "Trainer",
    "Score",
    "Role",
]


def details_rows(view, ranked, odds_lookup=None) -> list[dict[str, Any]]:
    runner_by = {getattr(r, "name", ""): r for r in (getattr(view, "runners", None) or [])}
    ranked_by = {getattr(rr, "name", ""): rr for rr in ranked or []}
    ordered = list(ranked or [])
    for runner in getattr(view, "runners", None) or []:
        if getattr(runner, "name", "") not in ranked_by:
            ordered.append(type("R", (), {"name": runner.name, "score": 0.0, "why_bullets": [], "key_factors": "", "debug": None})())
    rows = []
    for rr in ordered:
        runner = runner_by.get(rr.name)
        no = program_number_for_runner(runner) if runner is not None else None
        role = ""
        tone = ""
        scratched = bool(getattr(runner, "scratched", False)) if runner else False
        sources = (getattr(view, "scratching_sources", None) or {}).get(rr.name) or []
        if getattr(runner, "raw", None) and isinstance(runner.raw, dict):
            extra = (runner.raw.get("_scratch") or {}).get("sources") or []
            for s in extra:
                if s not in sources:
                    sources.append(s)
        if scratched:
            tone = "scratch"
            role = "SCR"
            if names_match(rr.name, getattr(view, "original_primary", "") or ""):
                role = "SCR ORIG"
        elif rr.name == view.primary:
            role = "PROMOTED" if getattr(view, "backup_promoted", False) else "PRIMARY"
            tone = "primary"
        elif rr.name == view.backup:
            role = "BACKUP"
            tone = "backup"
        odds_val = None
        fluc = ""
        flucs: list = []
        if odds_lookup:
            o = odds_lookup(view.venue_raw or view.venue, view.race_no, rr.name)
            if o:
                odds_val = o.get("win")
                fluc = str(o.get("fluc") or "")
                flucs = list(o.get("flucs") or [])
        elif view.primary == rr.name:
            odds_val = view.odds
        odds_txt = "—"
        if odds_val is not None:
            try:
                odds_txt = f"${float(odds_val):.2f}"
            except (TypeError, ValueError):
                odds_txt = "—"
        last_class = runner_last_class(runner) if runner else ""
        arrow = runner_class_arrow(runner, view.race_class) if runner else ""
        rows.append(
            {
                "no": "" if no is None else str(no),
                "no_value": no,
                "barrier": "" if runner is None or runner.draw is None else str(runner.draw),
                "barrier_value": None if runner is None else runner.draw,
                "name": format_runner_pick(no, rr.name),
                "raw_name": rr.name,
                "odds": odds_txt,
                "odds_value": odds_val,
                "fluc": fluc,
                "flucs": flucs,
                "form": getattr(runner, "last10", None) or "—",
                "class": f"{last_class} {arrow}".strip() or "—",
                "last_class": last_class,
                "class_arrow": arrow,
                "class_label": view.race_class,
                "weight": "—" if runner is None or runner.weight_kg is None else str(runner.weight_kg),
                "weight_value": None if runner is None else runner.weight_kg,
                "jockey": (getattr(runner, "jockey_or_driver", None) or "—") if runner else "—",
                "trainer": (getattr(runner, "trainer", None) or "—") if runner else "—",
                "score": "—" if scratched else f"{float(getattr(rr, 'score', 0.0) or 0.0):.3f}",
                "score_value": None if scratched else float(getattr(rr, "score", 0.0) or 0.0),
                "status": "SCRATCHED" if scratched else (role or "—"),
                "role": role,
                "row_tone": tone,
                "silk": getattr(runner, "silk_url", None) or "",
                "scratched": scratched,
                "scratch_sources": sources,
                "why": list(getattr(rr, "why_bullets", []) or []),
                "debug": getattr(rr, "debug", None),
                "key_factors": (getattr(rr, "key_factors", "") or "") if not scratched else (f"Scratched via {', '.join(sources) or 'unknown'}"),
                "profile_url": getattr(runner, "profile_url", None) or "",
                "age": getattr(runner, "age", None) if runner else None,
                "sex": getattr(runner, "sex", None) if runner else "",
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
        col = index.column()
        keys = [
            "silk",
            "no",
            "name",
            "barrier",
            "odds",
            "fluc",
            "form",
            "class",
            "weight",
            "jockey",
            "trainer",
            "score",
            "status",
        ]
        if role == Qt.ItemDataRole.DisplayRole:
            if col == 0:
                return ""
            return row.get(keys[col], "")
        if role == Qt.ItemDataRole.ToolTipRole:
            if row.get("scratched") and (row.get("scratch_sources") or row.get("key_factors")):
                src = ", ".join(row.get("scratch_sources") or []) or "unknown"
                return f"Scratched via {src}"
            if col in {6, 9, 10}:
                return str(row.get(keys[col], "") or "")
            if col == 7:
                return f"{row.get('last_class') or ''} {row.get('class_arrow') or ''} today {row.get('class_label') or ''}".strip()
            return str(row.get(keys[col], "") or row.get("raw_name") or "")
        if role == Qt.ItemDataRole.UserRole:
            return row
        if role == ROW_TONE_ROLE:
            return row.get("row_tone")
        if role == SILK_URL_ROLE:
            return row.get("silk") or ""
        if role == PROGRAM_NUMBER_ROLE:
            return row.get("no_value")
        if role == HORSE_NAME_ROLE:
            return row.get("raw_name")
        if role == BARRIER_ROLE:
            return row.get("barrier_value")
        if role == ODDS_ROLE:
            return row.get("odds_value")
        if role == FLUCTUATION_ROLE:
            return row.get("fluc")
        if role == FLUCTUATION_HISTORY_ROLE:
            return row.get("flucs")
        if role == FORM_ROLE:
            return row.get("form")
        if role == CLASS_ARROW_ROLE:
            return row.get("class_arrow")
        if role == CLASS_LABEL_ROLE:
            return row.get("class_label")
        if role == LAST_CLASS_ROLE:
            return row.get("last_class")
        if role == WEIGHT_ROLE:
            return row.get("weight_value")
        if role == JOCKEY_ROLE:
            return row.get("jockey")
        if role == TRAINER_ROLE:
            return row.get("trainer")
        if role == SCORE_ROLE:
            return row.get("score_value")
        if role == PICK_ROLE:
            if row.get("role") == "PRIMARY":
                return "primary"
            if row.get("role") == "BACKUP":
                return "backup"
            return ""
        if role == SCRATCHED_ROLE:
            return row.get("scratched")
        if role == RESULT_STATUS_ROLE and col == 12:
            return row.get("status")
        if role == WHY_ROLE:
            return row.get("why")
        if role == PROFILE_URL_ROLE:
            return row.get("profile_url")
        if role == DETAIL_ROLE:
            return row
        if role == SORT_ROLE:
            if col == 1:
                n = row.get("no_value")
                return int(n) if n is not None else 10**6
            if col == 3:
                b = row.get("barrier_value")
                return int(b) if b is not None else 10**6
            if col in {4, 5}:
                v = row.get("odds_value")
                return float(v) if v is not None else 10**6
            if col == 11:
                return float(row.get("score_value") or 0)
            return row.get(keys[col], "")
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
        return [row.get("silk") for row in self._rows if row.get("silk")]

    def indexes_for_silk(self, url: str) -> list[QModelIndex]:
        return [self.index(i, 0) for i, row in enumerate(self._rows) if row.get("silk") == url]

    def row_at(self, row: int) -> Optional[dict[str, Any]]:
        if 0 <= row < len(self._rows):
            return self._rows[row]
        return None
