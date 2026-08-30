"""QMainWindow shell for Race Day Rater."""

from __future__ import annotations

from datetime import date

from PySide6.QtCore import QDate, Slot
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QToolBar,
)

from desktop import APP_NAME
from desktop.application_controller import ApplicationController
from desktop.models.picks_table_model import pick_rows_from_views
from desktop.models.race_table_model import race_to_row
from desktop.settings import STATES
from desktop.status import LOADING_MESSAGE, empty_label
from desktop.themes.theme_manager import apply_to_application, refresh_styled_widgets
from desktop.widgets.history_page import HistoryPage
from desktop.widgets.model_page import ModelPage
from desktop.widgets.race_day_page import RaceDayPage
from desktop.widgets.race_details_page import RaceDetailsPage
from desktop.widgets.settings_page import SettingsPage
from desktop.widgets.status_bar import StatusBarHost


PAGES = ["Race Day", "Race Details", "History", "Model", "Settings"]


class MainWindow(QMainWindow):
    def __init__(self, controller: ApplicationController | None = None, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(APP_NAME)
        self.controller = controller or ApplicationController()
        self._status = StatusBarHost(self.statusBar())

        self.nav = QListWidget()
        self.nav.setFixedWidth(148)
        for name in PAGES:
            QListWidgetItem(name, self.nav)
        self.stack = QStackedWidget()
        self.race_day = RaceDayPage()
        self.details = RaceDetailsPage()
        self.history = HistoryPage()
        self.model = ModelPage()
        self.settings_page = SettingsPage(self.controller.settings)
        for w in (self.race_day, self.details, self.history, self.model, self.settings_page):
            self.stack.addWidget(w)

        split = QSplitter()
        split.addWidget(self.nav)
        split.addWidget(self.stack)
        split.setStretchFactor(1, 1)
        self.setCentralWidget(split)

        self._build_toolbar()
        self._wire()
        self._restore_geometry()
        last = self.controller.settings.last_page
        idx = PAGES.index(last) if last in PAGES else 0
        self.nav.setCurrentRow(idx)
        self.stack.setCurrentIndex(idx)
        if idx == 2:
            self.history.set_range(self.controller.chosen_date, self.controller.settings.db_path)
        self.race_day.empty.setText(LOADING_MESSAGE)
        self._apply_theme(self.controller.settings.theme)

    def _build_toolbar(self) -> None:
        bar = QToolBar("Race Day")
        bar.setMovable(False)
        self.addToolBar(bar)
        self.date_edit = QDateEdit()
        self.date_edit.setCalendarPopup(True)
        self.date_edit.setDisplayFormat("yyyy-MM-dd")
        today = self.controller.chosen_date
        self.date_edit.blockSignals(True)
        self.date_edit.setDate(QDate(today.year, today.month, today.day))
        self.date_edit.blockSignals(False)
        self.state = QComboBox()
        self.state.addItems(list(STATES))
        self.state.blockSignals(True)
        self.state.setCurrentText(self.controller.settings.state_filter)
        self.state.blockSignals(False)
        self.auto = QCheckBox("Auto-refresh")
        self.auto.setChecked(self.controller.settings.auto_refresh)
        self.refresh_btn = QPushButton("Refresh")
        self.last_ok = QLabel("Last refresh —")
        self.last_ok.setObjectName("muted")
        self.health = QLabel("● Idle")
        bar.addWidget(QLabel(" Date "))
        bar.addWidget(self.date_edit)
        bar.addWidget(QLabel(" State "))
        bar.addWidget(self.state)
        bar.addWidget(self.auto)
        bar.addWidget(self.refresh_btn)
        bar.addSeparator()
        bar.addWidget(self.last_ok)
        bar.addWidget(self.health)

    def _wire(self) -> None:
        c = self.controller
        self.nav.currentRowChanged.connect(self._page_changed)
        self.date_edit.dateChanged.connect(self._date_changed)
        self.state.currentTextChanged.connect(self._state_changed)
        self.auto.toggled.connect(self._auto_toggled)
        self.refresh_btn.clicked.connect(self._manual_refresh)
        c.status_changed.connect(self._status.set_message)
        c.health_changed.connect(self._health)
        c.views_changed.connect(self._apply_views)
        c.clock_ticked.connect(self._tick)
        c.notify.connect(self._toast)
        c.refresh_busy_changed.connect(self._refresh_busy)
        self.race_day.open_race.connect(self._open_race)
        self.race_day.lock_hero.connect(lambda: c.lock_view(self.race_day.hero.view))
        self.race_day.lock_race.connect(self._lock_race)
        self.details.lock_pick.connect(lambda: c.lock_view(c.view_for_key(c.selected_key)))
        self.details.refresh_race.connect(self._manual_refresh)
        self.settings_page.settings_saved.connect(self.controller.apply_settings)
        self.settings_page.theme_changed.connect(self._apply_theme)
        self.settings_page.reset_db_requested.connect(self._reset_db_path)

    def _manual_refresh(self) -> None:
        self.controller.request_manual_refresh()

    @Slot(bool)
    def _refresh_busy(self, busy: bool) -> None:
        self.refresh_btn.setEnabled(not busy)
        self.refresh_btn.setText("Refreshing…" if busy else "Refresh")

    def _page_changed(self, row: int) -> None:
        self.stack.setCurrentIndex(max(0, row))
        name = PAGES[row] if 0 <= row < len(PAGES) else "Race Day"
        self.controller.settings.last_page = name
        if name == "History":
            self.history.set_range(self.controller.chosen_date, self.controller.settings.db_path)
        if name == "Settings":
            self.settings_page.load_into_widgets()
            self.settings_page.set_diagnostics(self.controller.diagnostics())

    def _reset_db_path(self) -> None:
        self.controller.settings.reset_db_path()
        self.controller.settings.sync()
        self.settings_page.load_into_widgets()
        self.settings_page.set_diagnostics(self.controller.diagnostics())
        self.controller.apply_settings()

    def _date_changed(self, qdate: QDate) -> None:
        d = date(qdate.year(), qdate.month(), qdate.day())
        self.controller.set_date(d)

    def _state_changed(self, state: str) -> None:
        self.controller.set_state(state)

    def _auto_toggled(self, on: bool) -> None:
        self.controller.set_auto_refresh(on)

    @Slot()
    def _apply_views(self) -> None:
        c = self.controller
        prev = self.race_day.selected_upcoming_key()
        state = c.sync_race_day()
        self._bind_hero_and_upcoming(state, restore_key=prev)
        index = {}
        for p in c.picks:
            try:
                index[(str(p.get("meeting_url") or ""), int(p.get("race_no") or 0))] = p
            except Exception:
                continue
        rows, summary = pick_rows_from_views(c.views, index, c.results, state.now)
        self.race_day.picks_model.set_rows(rows)
        self.race_day.picks.prefetch_silks()
        self.race_day.set_summary(summary)
        view = c.view_for_key(c.selected_key) or state.hero
        if view is not None:
            self.details.set_view(view, odds_lookup=c.odds_lookup())
        self.race_day.empty.setText(empty_label(c.stage, bool(c.views), c.last_error))
        self.settings_page.set_diagnostics(c.diagnostics())
        self._restore_column_widths()

    def _bind_hero_and_upcoming(self, state, *, restore_key=None) -> None:
        now = state.now
        lookup = self.controller.odds_lookup()
        self.race_day.upcoming_model.set_rows([race_to_row(v, now, odds_lookup=lookup) for v in state.upcoming])
        self.race_day.upcoming.prefetch_silks()
        self.race_day.hero.set_view(state.hero, now)
        if restore_key:
            self.race_day.restore_selection(restore_key)

    def _lock_race(self, key) -> None:
        self.controller.lock_view(self.controller.view_for_key(key))

    def _apply_theme(self, ident: str | None = None) -> None:
        app = QApplication.instance()
        wanted = ident or self.controller.settings.theme
        if app is not None:
            apply_to_application(app, wanted)
        refresh_styled_widgets(self)

    @Slot()
    def _tick(self) -> None:
        state = self.controller.last_race_day_state
        if state is None:
            state = self.controller.sync_race_day()
        now = state.now
        shown = self.race_day.hero.view
        shown_key = shown.race_key if shown is not None else None
        new_key = state.hero.race_key if state.hero is not None else None
        if shown_key != new_key:
            prev = self.race_day.selected_upcoming_key()
            self._bind_hero_and_upcoming(state, restore_key=prev)
        else:
            self.race_day.hero.tick(now)
            self.race_day.upcoming_model.update_countdowns(now)

    def _open_race(self, key) -> None:
        view = self.controller.select_race(key)
        if view is None:
            return
        self.details.set_view(view, odds_lookup=self.controller.odds_lookup())
        self.nav.setCurrentRow(1)

    @Slot(str, str)
    def _health(self, token: str, last_ok: str) -> None:
        labels = {
            "ok": ("● Data OK", "healthOk"),
            "warn": ("● Partial / cached", "healthWarn"),
            "cached": ("● Cached — refreshing", "healthWarn"),
            "error": ("● Refresh failed", "healthErr"),
            "idle": ("● Idle", "muted"),
        }
        text, obj = labels.get(token, ("● Idle", "muted"))
        self.health.setText(text)
        self.health.setObjectName(obj)
        self.health.style().unpolish(self.health)
        self.health.style().polish(self.health)
        self.last_ok.setText(f"Last successful refresh {last_ok}" if last_ok else "Last refresh —")

    @Slot(str, str)
    def _toast(self, title: str, body: str) -> None:
        self._status.set_message(f"{title}: {body}")

    def _restore_geometry(self) -> None:
        geo = self.controller.settings.geometry()
        if geo:
            self.restoreGeometry(geo)
        else:
            self.resize(1280, 800)

    def _restore_column_widths(self) -> None:
        for name, view in self._tables():
            view.apply_column_widths(self.controller.settings.column_widths(name))

    def _save_column_widths(self) -> None:
        for name, view in self._tables():
            self.controller.settings.set_column_widths(name, view.current_column_widths())

    def _tables(self):
        return (
            ("upcoming", self.race_day.upcoming),
            ("picks", self.race_day.picks),
            ("details", self.details.table),
            ("history", self.history.table),
        )

    def closeEvent(self, event: QCloseEvent) -> None:
        self.controller.settings.set_geometry(self.saveGeometry())
        self._save_column_widths()
        self.controller.settings.sync()
        self.controller.shutdown()
        super().closeEvent(event)
