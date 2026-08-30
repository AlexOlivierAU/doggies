"""Desktop settings and diagnostics."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from desktop.paths import desktop_log_path, shared_default_db_path
from desktop.settings import STATES, TIMEZONES, DesktopSettings
from desktop.themes import THEME_CHOICES
from race_db import db_status


class SettingsPage(QWidget):
    settings_saved = Signal()
    reset_db_requested = Signal()
    theme_changed = Signal(str)

    def __init__(self, settings: DesktopSettings, parent=None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.timezone = QComboBox()
        self.timezone.addItems(list(TIMEZONES))
        self.state = QComboBox()
        self.state.addItems(list(STATES))
        self.auto = QCheckBox("Automatic background refresh")
        self.odds = QSpinBox()
        self.odds.setRange(15, 180)
        self.odds.setSuffix(" s")
        self.fields = QSpinBox()
        self.fields.setRange(60, 600)
        self.fields.setSuffix(" s")
        self.results = QSpinBox()
        self.results.setRange(15, 180)
        self.results.setSuffix(" s")
        self.notify = QCheckBox("In-app notifications")
        self.theme = QComboBox()
        for ident, label in THEME_CHOICES:
            self.theme.addItem(label, ident)
        self.other = QCheckBox("Enable other racing codes (greyhound/harness) — Streamlit roster only")
        self.db = QLineEdit()
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        db_row = QHBoxLayout()
        db_row.addWidget(self.db, 1)
        db_row.addWidget(browse)
        self.db_info = QLabel("")
        self.db_info.setObjectName("muted")
        self.db_info.setWordWrap(True)

        form = QFormLayout()
        form.addRow("Timezone", self.timezone)
        form.addRow("State filter", self.state)
        form.addRow("", self.auto)
        form.addRow("Odds / scratchings interval", self.odds)
        form.addRow("Meetings / fields interval", self.fields)
        form.addRow("Results interval", self.results)
        form.addRow("", self.notify)
        form.addRow("Theme", self.theme)
        self.theme_note = QLabel("Applies immediately. Blue is the default for new installs.")
        self.theme_note.setObjectName("muted")
        form.addRow("", self.theme_note)
        form.addRow("", self.other)
        form.addRow("Database", db_row)

        save = QPushButton("Save settings")
        save.setObjectName("primaryButton")
        save.clicked.connect(self.apply_from_widgets)

        self.diag = QLabel("")
        self.diag.setObjectName("muted")
        self.diag.setWordWrap(True)
        self.diag.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        reset_db = QPushButton("Reset database path to default")
        reset_db.clicked.connect(self.reset_db_requested.emit)
        open_logs = QPushButton("Open log folder")
        open_logs.clicked.connect(self._open_log_folder)

        root = QVBoxLayout(self)
        root.addWidget(QLabel("Settings"))
        root.addLayout(form)
        root.addWidget(self.db_info)
        root.addWidget(save)
        root.addWidget(QLabel("Diagnostics"))
        root.addWidget(self.diag)
        diag_btns = QHBoxLayout()
        diag_btns.addWidget(reset_db)
        diag_btns.addWidget(open_logs)
        diag_btns.addStretch(1)
        root.addLayout(diag_btns)
        root.addStretch(1)
        self.load_into_widgets()
        self.theme.currentIndexChanged.connect(self._live_theme)

    def load_into_widgets(self) -> None:
        self.timezone.setCurrentText(self.settings.timezone)
        self.state.setCurrentText(self.settings.state_filter)
        self.auto.setChecked(self.settings.auto_refresh)
        self.odds.setValue(self.settings.interval_odds_sec)
        self.fields.setValue(self.settings.interval_fields_sec)
        self.results.setValue(self.settings.interval_results_sec)
        self.notify.setChecked(self.settings.notifications)
        ident = self.settings.theme
        idx = self.theme.findData(ident)
        self.theme.blockSignals(True)
        self.theme.setCurrentIndex(idx if idx >= 0 else 0)
        self.theme.blockSignals(False)
        self.other.setChecked(self.settings.other_codes_enabled)
        self.db.setText(str(self.settings.db_path))
        self._refresh_db_info()

    def apply_from_widgets(self) -> None:
        self.settings.timezone = self.timezone.currentText()
        self.settings.state_filter = self.state.currentText()
        self.settings.auto_refresh = self.auto.isChecked()
        self.settings.interval_odds_sec = self.odds.value()
        self.settings.interval_fields_sec = self.fields.value()
        self.settings.interval_results_sec = self.results.value()
        self.settings.notifications = self.notify.isChecked()
        ident = self.theme.currentData() or self.settings.theme
        self.settings.theme = ident
        self.settings.other_codes_enabled = self.other.isChecked()
        self.settings.db_path = Path(self.db.text().strip() or self.settings.db_path)
        self.settings.sync()
        self._refresh_db_info()
        self.theme_changed.emit(str(ident))
        self.settings_saved.emit()

    def _live_theme(self) -> None:
        ident = self.theme.currentData()
        if not ident:
            return
        self.settings.theme = ident
        self.settings.sync()
        self.theme_changed.emit(str(ident))

    def set_diagnostics(self, info: dict | None) -> None:
        info = info or {}
        exists = "yes" if info.get("db_exists") else "no"
        warning = info.get("db_warning") or ""
        obsolete = info.get("obsolete_db_path") or ""
        lines = [
            f"Resolved database path: {info.get('db_path') or self.settings.db_path}",
            f"Database exists: {exists}",
            f"Application default: {info.get('default_db_path') or shared_default_db_path()}",
            f"Cached meetings count for selected date: {info.get('cached_meetings', '—')}",
            f"Cached field count: {info.get('cached_fields', '—')}",
            f"Last refresh stage: {info.get('stage') or '—'}",
            f"Last error summary: {info.get('last_error') or '—'}",
            f"Desktop log path: {info.get('log_path') or desktop_log_path()}",
        ]
        if obsolete:
            lines.insert(2, f"Obsolete/invalid configured path: {obsolete}")
        if warning:
            lines.insert(2, warning)
        self.diag.setText("\n".join(lines))

    def _browse(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Database file", self.db.text(), "SQLite (*.db);;All (*)")
        if path:
            self.db.setText(path)

    def _open_log_folder(self) -> None:
        folder = desktop_log_path().parent
        folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def _refresh_db_info(self) -> None:
        try:
            info = db_status(self.settings.db_path)
            self.db_info.setText(
                f"{info.get('path')}  ·  picks {info.get('picks', 0)}  ·  results {info.get('results', 0)}  ·  "
                f"fields {info.get('daily_fields', 0)}  ·  meetings {info.get('daily_meetings', 0)}"
            )
        except Exception:
            self.db_info.setText("Could not read database status.")
