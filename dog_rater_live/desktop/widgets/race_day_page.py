"""Race Day: next race, upcoming table, today's picks."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from desktop.models.picks_table_model import PicksTableModel
from desktop.models.race_table_model import RACE_KEY_ROLE, RaceTableModel
from desktop.widgets.next_race_card import NextRaceCard


class _Stat(QFrame):
    def __init__(self, label: str, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("statCard")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        self.kicker = QLabel(label)
        self.kicker.setObjectName("kicker")
        self.value = QLabel("—")
        self.value.setObjectName("pickLine")
        lay.addWidget(self.kicker)
        lay.addWidget(self.value)

    def set_value(self, text: str) -> None:
        self.value.setText(text)


class RaceDayPage(QWidget):
    open_race = Signal(object)
    lock_hero = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.hero = NextRaceCard()
        self.hero.open_race.connect(lambda: self.open_race.emit(self.hero.view))
        self.hero.lock_pick.connect(self.lock_hero.emit)

        self.empty = QLabel("")
        self.empty.setObjectName("muted")
        self.empty.setWordWrap(True)

        self.upcoming_model = RaceTableModel(self)
        self.upcoming = QTableView()
        self.upcoming.setModel(self.upcoming_model)
        self.upcoming.setAlternatingRowColors(True)
        self.upcoming.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.upcoming.setSelectionMode(QTableView.SelectionMode.SingleSelection)
        self.upcoming.setSortingEnabled(False)
        self.upcoming.verticalHeader().setVisible(False)
        self.upcoming.horizontalHeader().setStretchLastSection(True)
        self.upcoming.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.upcoming.setMinimumHeight(180)
        self.upcoming.doubleClicked.connect(self._open_upcoming)
        self.upcoming.activated.connect(self._open_upcoming)

        self.stats = [_Stat(x) for x in ("Completed", "Primary wins", "Primary places", "Backup wins", "Win SR", "Place SR")]
        self.return_label = QLabel("")
        self.return_label.setObjectName("muted")

        self.picks_model = PicksTableModel(self)
        self.picks = QTableView()
        self.picks.setModel(self.picks_model)
        self.picks.setAlternatingRowColors(True)
        self.picks.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.picks.setSelectionMode(QTableView.SelectionMode.SingleSelection)
        self.picks.verticalHeader().setVisible(False)
        self.picks.horizontalHeader().setStretchLastSection(True)
        self.picks.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)

        root = QVBoxLayout(self)
        root.addWidget(self.hero)
        root.addWidget(self.empty)
        root.addWidget(QLabel("Upcoming"))
        root.addWidget(self.upcoming, 2)
        root.addWidget(QLabel("Today's picks"))
        stats_row = QHBoxLayout()
        for s in self.stats:
            stats_row.addWidget(s)
        root.addLayout(stats_row)
        root.addWidget(self.return_label)
        root.addWidget(self.picks, 2)

    def _open_upcoming(self, index) -> None:
        key = self.upcoming_model.data(self.upcoming_model.index(index.row(), 0), RACE_KEY_ROLE)
        self.open_race.emit(key)

    def restore_selection(self, race_key) -> None:
        row = self.upcoming_model.find_row(race_key)
        if row >= 0:
            self.upcoming.selectRow(row)
            self.upcoming.scrollTo(self.upcoming_model.index(row, 0))

    def selected_upcoming_key(self):
        idx = self.upcoming.currentIndex()
        if not idx.isValid():
            return None
        return self.upcoming_model.data(self.upcoming_model.index(idx.row(), 0), RACE_KEY_ROLE)

    def set_summary(self, summary) -> None:
        self.stats[0].set_value(str(summary.completed))
        self.stats[1].set_value(str(summary.primary_wins))
        self.stats[2].set_value(str(summary.primary_places))
        self.stats[3].set_value(str(summary.backup_wins))
        self.stats[4].set_value(f"{summary.win_strike_rate:.0%}" if summary.win_strike_rate is not None else "—")
        self.stats[5].set_value(f"{summary.place_strike_rate:.0%}" if summary.place_strike_rate is not None else "—")
        if summary.estimated_win_return is not None:
            self.return_label.setText(f"{summary.estimated_return_label}: {summary.estimated_win_return:+.2f}u")
        else:
            self.return_label.setText("Financial return omitted — saved odds are incomplete.")
