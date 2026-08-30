"""Race Day: next race, upcoming table, today's picks."""

from __future__ import annotations

from PySide6.QtCore import QUrl, Qt, Signal
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QMenu, QVBoxLayout, QWidget

from desktop.models.picks_table_model import PicksTableModel
from desktop.models.race_table_model import RaceTableModel
from desktop.widgets.next_race_card import NextRaceCard
from desktop.widgets.runner_details_dialog import show_runner_details
from desktop.widgets.styled_table import StyledTableView


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
    lock_race = Signal(object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.hero = NextRaceCard()
        self.hero.open_race.connect(lambda: self.open_race.emit(self.hero.view))
        self.hero.lock_pick.connect(self.lock_hero.emit)

        self.empty = QLabel("")
        self.empty.setObjectName("muted")
        self.empty.setWordWrap(True)

        self.upcoming_model = RaceTableModel(self)
        self.upcoming = StyledTableView(self, sorting=True, name="upcoming")
        self.upcoming.set_source_model(self.upcoming_model)
        self.upcoming.set_pick_columns([4, 6], compact=True)
        self.upcoming.set_odds_columns([5])
        self.upcoming.set_badge_columns([7, 8])
        self.upcoming.setMinimumHeight(180)
        self.upcoming.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        self.upcoming.row_activated.connect(self._open_row)
        self.upcoming.context_row.connect(self._upcoming_menu)

        self.stats = [_Stat(x) for x in ("Completed", "Primary wins", "Primary places", "Backup wins", "Win SR", "Place SR")]
        self.return_label = QLabel("")
        self.return_label.setObjectName("muted")

        self.picks_model = PicksTableModel(self)
        self.picks = StyledTableView(self, sorting=True, name="picks")
        self.picks.set_source_model(self.picks_model)
        self.picks.set_pick_columns([4, 7], compact=True)
        self.picks.set_odds_columns([6])
        self.picks.set_badge_columns([0, 9])
        self.picks.row_activated.connect(self._open_pick_row)
        self.picks.context_row.connect(self._picks_menu)

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

    def _open_row(self, row) -> None:
        if row:
            self.open_race.emit(row.get("race_key"))

    def _open_pick_row(self, row) -> None:
        if row:
            self.open_race.emit(row.get("race_key"))

    def restore_selection(self, race_key) -> None:
        self.upcoming.restore_selection(race_key)

    def selected_upcoming_key(self):
        return self.upcoming.selected_key()

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

    def _upcoming_menu(self, row, pos) -> None:
        if not row:
            return
        menu = QMenu(self)
        open_act = menu.addAction("Open race")
        lock_act = menu.addAction("Confirm / lock pick")
        src_act = menu.addAction("Open source page")
        menu.addSeparator()
        copy_horse = menu.addAction("Copy horse name")
        copy_sum = menu.addAction("Copy race summary")
        chosen = menu.exec(pos)
        if chosen is open_act:
            self.open_race.emit(row.get("race_key"))
        elif chosen is lock_act:
            self.lock_race.emit(row.get("race_key"))
        elif chosen is src_act:
            url = row.get("race_url") or ""
            if url:
                QDesktopServices.openUrl(QUrl(url))
        elif chosen is copy_horse:
            QGuiApplication.clipboard().setText(str(row.get("primary_name") or ""))
        elif chosen is copy_sum:
            QGuiApplication.clipboard().setText(
                f"{row.get('venue')} {row.get('race')} {row.get('jump')} {row.get('primary')}"
            )

    def _picks_menu(self, row, pos) -> None:
        if not row:
            return
        menu = QMenu(self)
        open_act = menu.addAction("Open race")
        primary_act = menu.addAction("Primary details")
        backup_act = menu.addAction("Backup details")
        chosen = menu.exec(pos)
        if chosen is open_act:
            self.open_race.emit(row.get("race_key"))
        elif chosen is primary_act:
            show_runner_details({**row, "role": "primary", "name": row.get("primary_name"), "silk": row.get("primary_silk")}, self)
        elif chosen is backup_act:
            show_runner_details({**row, "role": "backup", "name": row.get("backup_name"), "silk": row.get("backup_silk")}, self)
