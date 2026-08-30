"""Race Details page."""

from __future__ import annotations

from PySide6.QtCore import QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QHBoxLayout, QLabel, QMenu, QPushButton, QTextEdit, QVBoxLayout, QWidget

from desktop.delegates.silk_pick_delegate import PickDelegate
from desktop.models.details_table_model import DetailsTableModel, details_rows
from desktop.widgets.runner_details_dialog import show_runner_details
from desktop.widgets.styled_table import StyledTableView
from services.formatting import format_runner_pick
from services.ranking import rank_field


class RaceDetailsPage(QWidget):
    lock_pick = Signal()
    refresh_race = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._view = None
        self.meta = QLabel("Select a race from Race Day.")
        self.meta.setWordWrap(True)
        self.picks = QLabel("")
        self.picks.setObjectName("pickLine")
        self.note = QLabel("")
        self.note.setObjectName("muted")
        self.model = DetailsTableModel(self)
        self.table = StyledTableView(self, sorting=True, name="details")
        self.table.set_source_model(self.model)
        self.table.set_silk_column(0)
        self.table.set_odds_columns([4, 5])
        self.table.set_class_columns([7])
        self.table.set_badge_columns([12])
        self.table.setItemDelegateForColumn(2, PickDelegate(self.table, compact=True, show_silk=False))
        self.table.row_activated.connect(self._open_detail)
        self.table.context_row.connect(self._menu)
        self.table.selectionModel().currentRowChanged.connect(self._row_changed)
        self.why = QTextEdit()
        self.why.setReadOnly(True)
        self.why.setPlaceholderText("Select a runner to see ranking reasons.")
        self.why.setMaximumHeight(140)

        self.lock_btn = QPushButton("Save / lock pick")
        self.refresh_btn = QPushButton("Refresh this race")
        self.source_btn = QPushButton("Open source page")
        self.lock_btn.clicked.connect(self.lock_pick.emit)
        self.refresh_btn.clicked.connect(self.refresh_race.emit)
        self.source_btn.clicked.connect(self._open_source)

        btns = QHBoxLayout()
        btns.addWidget(self.lock_btn)
        btns.addWidget(self.refresh_btn)
        btns.addWidget(self.source_btn)
        btns.addStretch(1)

        root = QVBoxLayout(self)
        root.addWidget(self.meta)
        root.addWidget(self.picks)
        root.addWidget(self.note)
        root.addLayout(btns)
        root.addWidget(self.table, 3)
        root.addWidget(QLabel("Ranking reasons"))
        root.addWidget(self.why)

    def set_view(self, view, odds_lookup=None) -> None:
        self._view = view
        if view is None:
            self.meta.setText("Select a race from Race Day.")
            self.picks.setText("")
            self.note.setText("")
            self.model.set_rows([])
            return
        dist = f"{view.distance_m}m" if view.distance_m else "—"
        self.meta.setText(
            f"{view.venue} R{view.race_no} · {view.clock()} · {dist} · {view.race_class or '—'} · "
            f"{view.track_condition or '—'} · field {view.field_size}"
        )
        self.picks.setText(
            f"Primary  {format_runner_pick(view.primary_no, view.primary, view.odds)}    "
            f"Backup  {format_runner_pick(view.backup_no, view.backup, view.backup_odds)}"
        )
        self.note.setText(
            "Saved snapshot — not a live re-rank of an old race."
            if view.from_snapshot
            else "Live ranking. Official No is the program number, not the barrier."
        )
        ranked, _w, _r = rank_field(view.runners, track_condition=view.meta.get("track_condition"))
        self.model.set_rows(details_rows(view, ranked, odds_lookup=odds_lookup))
        self.table.prefetch_silks()
        self.lock_btn.setEnabled(bool(view.primary) and not view.locked)
        if self.model.rowCount():
            src = self.model.index(0, 0)
            mapped = self.table.proxy.mapFromSource(src)
            self.table.selectRow(mapped.row())
            self._fill_why(self.model.row_at(0))

    def _row_changed(self, current, _prev) -> None:
        self._fill_why(self.table.source_row(current))

    def _open_detail(self, row) -> None:
        if not row:
            return
        self._fill_why(row)
        show_runner_details(row, self)

    def _menu(self, row, pos) -> None:
        if not row:
            return
        menu = QMenu(self)
        act = menu.addAction("Runner details")
        src = menu.addAction("Open horse form")
        chosen = menu.exec(pos)
        if chosen is act:
            show_runner_details(row, self)
        elif chosen is src:
            url = row.get("profile_url") or (self._view.race_url if self._view else "")
            if url:
                QDesktopServices.openUrl(QUrl(url))

    def _fill_why(self, row) -> None:
        if not row:
            self.why.clear()
            return
        lines = [
            f"{row.get('raw_name')} · barrier {row.get('barrier') or '—'} · No {row.get('no') or '—'}",
            row.get("key_factors") or "",
        ]
        for b in row.get("why") or []:
            lines.append(f"• {b}")
        self.why.setPlainText("\n".join(x for x in lines if x))

    def _open_source(self) -> None:
        if self._view and self._view.race_url:
            QDesktopServices.openUrl(QUrl(self._view.race_url))
