"""History of immutable saved snapshots."""

from __future__ import annotations

from datetime import date

from PySide6.QtCore import QDate
from PySide6.QtWidgets import (
    QComboBox,
    QDateEdit,
    QHBoxLayout,
    QLabel,
    QHeaderView,
    QTableView,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from desktop.models.picks_table_model import PicksTableModel
from race_db import get_pick, load_picks_range, load_results_range
from services.confidence import LABEL_CLOSE, LABEL_MEDIUM, LABEL_STRONG
from services.formatting import format_saved_selection
from services.result_service import (
    AWAITING_RESULT,
    BACKUP_WON,
    LOST,
    PENDING,
    PLACED,
    PRIMARY_SCRATCHED,
    RESULT_UNAVAILABLE,
    VOID,
    WIN,
    resolve_pick_result,
)

_STATUSES = [
    "All",
    PENDING,
    AWAITING_RESULT,
    WIN,
    PLACED,
    LOST,
    PRIMARY_SCRATCHED,
    BACKUP_WON,
    VOID,
    RESULT_UNAVAILABLE,
]


class HistoryPage(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._db_path = None
        self.date_from = QDateEdit()
        self.date_to = QDateEdit()
        self.date_from.setCalendarPopup(True)
        self.date_to.setCalendarPopup(True)
        self.venue = QComboBox()
        self.status = QComboBox()
        self.status.addItems(_STATUSES)
        self.conf = QComboBox()
        self.conf.addItems(["All", LABEL_STRONG, LABEL_MEDIUM, LABEL_CLOSE])
        self.model = PicksTableModel(self)
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.selectionModel().currentRowChanged.connect(self._show_snapshot)
        self.detail = QTextEdit()
        self.detail.setReadOnly(True)
        self.note = QLabel("Saved snapshots only. Old races are not re-scored with the current model.")
        self.note.setObjectName("muted")

        for w in (self.date_from, self.date_to, self.venue, self.status, self.conf):
            if hasattr(w, "dateChanged"):
                w.dateChanged.connect(self.reload)
            if hasattr(w, "currentTextChanged"):
                w.currentTextChanged.connect(self.reload)

        filters = QHBoxLayout()
        filters.addWidget(QLabel("From"))
        filters.addWidget(self.date_from)
        filters.addWidget(QLabel("To"))
        filters.addWidget(self.date_to)
        filters.addWidget(QLabel("Venue"))
        filters.addWidget(self.venue)
        filters.addWidget(QLabel("Result"))
        filters.addWidget(self.status)
        filters.addWidget(QLabel("Confidence"))
        filters.addWidget(self.conf)
        filters.addStretch(1)

        root = QVBoxLayout(self)
        root.addWidget(self.note)
        root.addLayout(filters)
        root.addWidget(self.table, 3)
        root.addWidget(QLabel("Saved snapshot"))
        root.addWidget(self.detail, 2)

    def set_range(self, today: date, db_path) -> None:
        self._db_path = db_path
        q = QDate(today.year, today.month, today.day)
        self.date_from.blockSignals(True)
        self.date_to.blockSignals(True)
        self.date_from.setDate(q.addDays(-7))
        self.date_to.setDate(q)
        self.date_from.blockSignals(False)
        self.date_to.blockSignals(False)
        self.reload()

    def reload(self) -> None:
        if self._db_path is None:
            return
        d0 = date(self.date_from.date().year(), self.date_from.date().month(), self.date_from.date().day())
        d1 = date(self.date_to.date().year(), self.date_to.date().month(), self.date_to.date().day())
        picks = load_picks_range(d0, d1, db_path=self._db_path)
        results = load_results_range(d0, d1, db_path=self._db_path)
        venues = sorted({str(p.get("venue") or "") for p in picks if p.get("venue")})
        current = self.venue.currentText()
        self.venue.blockSignals(True)
        self.venue.clear()
        self.venue.addItem("All")
        self.venue.addItems(venues)
        idx = self.venue.findText(current)
        if idx >= 0:
            self.venue.setCurrentIndex(idx)
        self.venue.blockSignals(False)

        venue_f = self.venue.currentText()
        status_f = self.status.currentText()
        conf_f = self.conf.currentText()
        rows = []
        for p in picks:
            if (p.get("code") or "thoroughbred") != "thoroughbred":
                continue
            if venue_f not in {"", "All"} and str(p.get("venue") or "") != venue_f:
                continue
            key = (
                str(p.get("meeting_date") or p.get("date") or ""),
                str(p.get("meeting_url") or ""),
                int(p.get("race_no") or 0),
            )
            result = results.get(key) or {}
            resolved = resolve_pick_result(p, result, jumped=True)
            rec = {
                **p,
                **resolved.as_dict(),
                "result": resolved.status,
                "jump": str(p.get("meeting_date") or p.get("date") or ""),
                "venue": p.get("venue") or "",
                "race": f"R{p.get('race_no')}",
                "primary": format_saved_selection(p, "primary"),
                "backup": format_saved_selection(p, "backup"),
                "primary_finish": resolved.primary_finish_label,
                "backup_finish": resolved.backup_finish_label,
                "saved_odds": p.get("primary_odds"),
                "confidence": p.get("confidence_label") or "",
                "source": resolved.result_source or resolved.match_note,
                "race_key": (p.get("meeting_url"), p.get("race_no"), p.get("meeting_date") or p.get("date")),
            }
            if status_f != "All" and rec["result"] != status_f:
                continue
            if conf_f != "All" and rec["confidence"] != conf_f:
                continue
            rows.append(rec)
        self.model.set_rows(rows)
        if rows:
            self.table.selectRow(0)
            self._show_snapshot(self.model.index(0, 0), None)
        else:
            self.detail.setPlainText("No snapshots match these filters.")

    def _show_snapshot(self, current, _prev) -> None:
        row = self.model.row_at(current.row()) if current.isValid() else None
        if not row or self._db_path is None:
            return
        try:
            d = date.fromisoformat(str(row.get("jump") or row.get("date") or row.get("meeting_date")))
        except Exception:
            self.detail.setPlainText(str(row.get("primary") or ""))
            return
        snap = get_pick(d, str(row.get("meeting_url") or ""), int(row.get("race_no") or 0), db_path=self._db_path)
        if not snap:
            self.detail.setPlainText("Could not load snapshot.")
            return
        field = (snap.get("snapshot") or {}).get("field") or []
        field_lines = []
        for f in field[:20]:
            n = f.get("program_number")
            name = f.get("name") or ""
            bar = f.get("draw")
            field_lines.append(f"  {n or '—'}  {name}  barrier {bar if bar is not None else '—'}")
        self.detail.setPlainText(
            "\n".join(
                [
                    f"Locked: {bool(snap.get('locked'))}",
                    f"Primary: {format_saved_selection(snap, 'primary')}",
                    f"Backup: {format_saved_selection(snap, 'backup')}",
                    f"Confidence: {snap.get('confidence_label')} (gap {snap.get('score_gap')})",
                    f"Odds at selection: {snap.get('primary_odds')} / backup {snap.get('backup_odds')}",
                    f"Primary scratched: {bool(snap.get('primary_scratched'))}  Backup promoted: {bool(snap.get('backup_promoted'))}",
                    f"Confirmed result: {row.get('result')}",
                    "",
                    "Saved field snapshot (numbers as captured, not live):",
                    *field_lines,
                ]
            )
        )
