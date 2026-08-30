"""QAbstractTableModel for the upcoming-races table."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt

from desktop.roles import (
    BARRIER_ROLE,
    CONFIDENCE_ROLE,
    DETAIL_ROLE,
    FLUCTUATION_HISTORY_ROLE,
    FLUCTUATION_ROLE,
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
    URGENCY_ROLE,
)
from desktop.table_theme import upcoming_tone
from services.formatting import format_clock, format_countdown, format_runner_pick
from services.names import names_match
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


def _runner_named(view: RaceView, name: str):
    want = (name or "").strip()
    if not want:
        return None
    for r in getattr(view, "runners", None) or []:
        if getattr(r, "name", "") == want or names_match(getattr(r, "name", ""), want):
            return r
    return None


def _odds_info(view: RaceView, name: str, odds_lookup=None, fallback=None) -> tuple[Any, str, list]:
    if odds_lookup and name:
        o = odds_lookup(view.venue_raw or view.venue, view.race_no, name)
        if o:
            return o.get("win"), str(o.get("fluc") or ""), list(o.get("flucs") or [])
    return fallback, "", []


def race_to_row(view: RaceView, now: datetime, odds_lookup=None) -> dict[str, Any]:
    primary_r = _runner_named(view, view.primary)
    backup_r = _runner_named(view, view.backup)
    p_odds, p_fluc, p_hist = _odds_info(view, view.primary, odds_lookup, view.odds)
    b_odds, b_fluc, b_hist = _odds_info(view, view.backup, odds_lookup, view.backup_odds)
    odds_txt = "—"
    if p_odds is not None:
        try:
            odds_txt = f"${float(p_odds):.2f}"
        except (TypeError, ValueError):
            odds_txt = "—"
    urgency = urgency_color(view, now)
    return {
        "jump": format_clock(view.jump_at),
        "countdown": format_countdown(view.jump_at, now),
        "venue": view.venue,
        "race": f"R{view.race_no}",
        "primary": format_runner_pick(view.primary_no, view.primary),
        "odds": odds_txt,
        "odds_value": p_odds if p_odds is not None else view.odds,
        "backup": format_runner_pick(view.backup_no, view.backup, b_odds),
        "confidence": view.confidence_label,
        "status": view.status,
        "jump_at": view.jump_at,
        "race_key": view.race_key,
        "urgency": urgency,
        "row_tone": upcoming_tone(urgency=urgency, status=view.status),
        "meeting_url": view.meeting_url,
        "race_url": view.race_url,
        "race_no": view.race_no,
        "primary_no": view.primary_no,
        "backup_no": view.backup_no,
        "primary_name": view.primary,
        "backup_name": view.backup,
        "primary_silk": getattr(primary_r, "silk_url", None) or "",
        "backup_silk": getattr(backup_r, "silk_url", None) or "",
        "primary_barrier": getattr(primary_r, "draw", None) if primary_r else None,
        "backup_barrier": getattr(backup_r, "draw", None) if backup_r else None,
        "primary_scratched": False,
        "backup_scratched": False,
        "primary_odds": p_odds,
        "backup_odds": b_odds,
        "primary_fluc": p_fluc,
        "backup_fluc": b_fluc,
        "primary_flucs": p_hist,
        "backup_flucs": b_hist,
        "primary_detail": _runner_detail(view, primary_r, "primary", p_odds, p_fluc),
        "backup_detail": _runner_detail(view, backup_r, "backup", b_odds, b_fluc),
        "barrier_note": "",
        "scratch_tip": getattr(view, "selection_warning", "") or "",
        "original_primary": getattr(view, "original_primary", "") or "",
        "original_backup": getattr(view, "original_backup", "") or "",
        "selection_warning": getattr(view, "selection_warning", "") or "",
    }


def _runner_detail(view, runner, role: str, odds, fluc: str) -> dict[str, Any]:
    if runner is None:
        return {"role": role, "name": "", "venue": view.venue, "race": f"R{view.race_no}"}
    from parse_racingaustralia import runner_class_arrow, runner_last_class

    return {
        "role": role,
        "name": getattr(runner, "name", "") or "",
        "no": str(getattr(runner, "program_number", "") or ""),
        "silk": getattr(runner, "silk_url", None) or "",
        "jockey": getattr(runner, "jockey_or_driver", None) or "",
        "trainer": getattr(runner, "trainer", None) or "",
        "barrier": getattr(runner, "draw", None),
        "weight": getattr(runner, "weight_kg", None),
        "last10": getattr(runner, "last10", None) or "",
        "last_class": runner_last_class(runner),
        "class_arrow": runner_class_arrow(runner, view.race_class),
        "class_label": view.race_class,
        "odds": odds,
        "fluc": fluc,
        "profile_url": getattr(runner, "profile_url", None) or "",
        "scratched": bool(getattr(runner, "scratched", False)),
        "venue": view.venue,
        "race": f"R{view.race_no}",
        "why": list(view.why or []) if role == "primary" else [],
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
        if role == Qt.ItemDataRole.ToolTipRole:
            if col in {4, 5, 6} and row.get("scratch_tip"):
                return row.get("scratch_tip")
            if col == 4:
                return _tooltip_pick(row, "primary")
            if col == 6:
                return _tooltip_pick(row, "backup")
            return str(row.get(keys[col], "") or "")
        if role == RACE_KEY_ROLE:
            return row.get("race_key")
        if role == URGENCY_ROLE or role == ROW_TONE_ROLE:
            return row.get("row_tone") or row.get("urgency")
        if role == CONFIDENCE_ROLE and col == 7:
            return row.get("confidence")
        if role == RESULT_STATUS_ROLE and col == 8:
            return str(row.get("status") or "").replace("_", " ").upper()
        if col == 4:
            return _pick_role(row, "primary", role)
        if col == 6:
            return _pick_role(row, "backup", role)
        if col == 5:
            if role == ODDS_ROLE:
                return row.get("odds_value")
            if role == FLUCTUATION_ROLE:
                return row.get("primary_fluc")
            if role == FLUCTUATION_HISTORY_ROLE:
                return row.get("primary_flucs")
            if role == SORT_ROLE:
                return row.get("odds_value") if row.get("odds_value") is not None else -1
        if role == SORT_ROLE:
            if col == 0:
                jump = row.get("jump_at")
                return jump.timestamp() if jump is not None else 0
            if col == 1:
                return str(row.get("countdown") or "")
            return row.get(keys[col], "")
        if role == DETAIL_ROLE:
            if col == 4:
                return row.get("primary_detail")
            if col == 6:
                return row.get("backup_detail")
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
        from services.race_day_service import urgency_color as _uc

        class _V:
            def __init__(self, row):
                self.scratching_warning = bool(row.get("scratching_warning"))
                self.status = row.get("status") or ""
                self.jump_at = row.get("jump_at")

        for row in self._rows:
            row["countdown"] = format_countdown(row.get("jump_at"), now)
            row["urgency"] = _uc(_V(row), now)
            row["row_tone"] = upcoming_tone(urgency=row["urgency"], status=row.get("status") or "")
        top = self.index(0, 1)
        bottom = self.index(len(self._rows) - 1, 1)
        self.dataChanged.emit(top, bottom, [Qt.ItemDataRole.DisplayRole, ROW_TONE_ROLE, URGENCY_ROLE])

    def silk_urls(self) -> list[str]:
        out = []
        for row in self._rows:
            for key in ("primary_silk", "backup_silk"):
                u = row.get(key) or ""
                if u:
                    out.append(u)
        return out

    def indexes_for_silk(self, url: str) -> list[QModelIndex]:
        found = []
        for i, row in enumerate(self._rows):
            if row.get("primary_silk") == url:
                found.append(self.index(i, 4))
            if row.get("backup_silk") == url:
                found.append(self.index(i, 6))
        return found

    def row_at(self, row: int) -> Optional[dict[str, Any]]:
        if 0 <= row < len(self._rows):
            return self._rows[row]
        return None

    def find_row(self, race_key) -> int:
        for i, row in enumerate(self._rows):
            if row.get("race_key") == race_key:
                return i
        return -1

    def race_keys(self) -> list:
        return [row.get("race_key") for row in self._rows]

    @property
    def rows(self) -> list[dict[str, Any]]:
        return list(self._rows)


def _pick_role(row: dict, which: str, role):
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
        return row.get(f"{prefix}_odds")
    if role == FLUCTUATION_ROLE:
        return row.get(f"{prefix}_fluc")
    if role == FLUCTUATION_HISTORY_ROLE:
        return row.get(f"{prefix}_flucs")
    if role == PICK_ROLE:
        return which
    if role == SCRATCHED_ROLE:
        return row.get(f"{prefix}_scratched")
    return None


def _tooltip_pick(row: dict, which: str) -> str:
    prefix = "primary" if which == "primary" else "backup"
    name = row.get(f"{prefix}_name") or ""
    no = row.get(f"{prefix}_no") or ""
    bar = row.get(f"{prefix}_barrier")
    odds = row.get(f"{prefix}_odds")
    fluc = row.get(f"{prefix}_fluc") or ""
    bits = [f"{no}. {name}".strip(". ") if no else name]
    if bar is not None:
        bits.append(f"barrier {bar}")
    if odds not in (None, ""):
        try:
            bits.append(f"${float(odds):.2f}{fluc}")
        except (TypeError, ValueError):
            pass
    return " · ".join(x for x in bits if x)
