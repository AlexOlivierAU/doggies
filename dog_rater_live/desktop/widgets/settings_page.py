"""Desktop settings."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Signal
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

from desktop.settings import STATES, TIMEZONES, DesktopSettings
from race_db import db_status


class SettingsPage(QWidget):
    settings_saved = Signal()

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
        self.theme.addItems(["dark"])
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
        form.addRow("", self.other)
        form.addRow("Database", db_row)

        save = QPushButton("Save settings")
        save.setObjectName("primaryButton")
        save.clicked.connect(self.apply_from_widgets)

        root = QVBoxLayout(self)
        root.addWidget(QLabel("Settings"))
        root.addLayout(form)
        root.addWidget(self.db_info)
        root.addWidget(save)
        root.addStretch(1)
        self.load_into_widgets()

    def load_into_widgets(self) -> None:
        self.timezone.setCurrentText(self.settings.timezone)
        self.state.setCurrentText(self.settings.state_filter)
        self.auto.setChecked(self.settings.auto_refresh)
        self.odds.setValue(self.settings.interval_odds_sec)
        self.fields.setValue(self.settings.interval_fields_sec)
        self.results.setValue(self.settings.interval_results_sec)
        self.notify.setChecked(self.settings.notifications)
        self.theme.setCurrentText(self.settings.theme)
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
        self.settings.theme = self.theme.currentText()
        self.settings.other_codes_enabled = self.other.isChecked()
        self.settings.db_path = Path(self.db.text().strip() or self.settings.db_path)
        self.settings.sync()
        self._refresh_db_info()
        self.settings_saved.emit()

    def _browse(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Database file", self.db.text(), "SQLite (*.db);;All (*)")
        if path:
            self.db.setText(path)

    def _refresh_db_info(self) -> None:
        try:
            info = db_status(self.settings.db_path)
            self.db_info.setText(
                f"{info.get('path')}  ·  picks {info.get('picks', 0)}  ·  results {info.get('results', 0)}  ·  "
                f"fields {info.get('daily_fields', 0)}  ·  meetings {info.get('daily_meetings', 0)}"
            )
        except Exception:
            self.db_info.setText("Could not read database status.")
