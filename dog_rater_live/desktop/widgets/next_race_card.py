"""Next-to-jump hero card."""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from services.formatting import format_runner_pick
from services.race_day_service import RaceView


class NextRaceCard(QFrame):
    open_race = Signal()
    lock_pick = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("heroCard")
        self._view: RaceView | None = None

        self.kicker = QLabel("NEXT TO JUMP")
        self.kicker.setObjectName("kicker")
        self.title = QLabel("No upcoming race")
        self.title.setObjectName("heroTitle")
        self.meta = QLabel("")
        self.meta.setObjectName("muted")
        self.primary = QLabel("—")
        self.primary.setObjectName("pickLine")
        self.backup = QLabel("—")
        self.backup.setObjectName("pickLine")
        self.confidence = QLabel("—")
        self.lock_state = QLabel("")
        self.lock_state.setObjectName("muted")
        self.warn = QLabel("")
        self.warn.setObjectName("warn")

        self.open_btn = QPushButton("Open race")
        self.lock_btn = QPushButton("Confirm / lock pick")
        self.lock_btn.setObjectName("primaryButton")
        self.open_btn.clicked.connect(self.open_race.emit)
        self.lock_btn.clicked.connect(self.lock_pick.emit)

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.addWidget(self.kicker)
        root.addWidget(self.title)
        root.addWidget(self.meta)

        grid = QGridLayout()
        grid.addWidget(QLabel("Primary"), 0, 0)
        grid.addWidget(self.primary, 0, 1)
        grid.addWidget(QLabel("Backup"), 1, 0)
        grid.addWidget(self.backup, 1, 1)
        grid.addWidget(QLabel("Confidence"), 2, 0)
        grid.addWidget(self.confidence, 2, 1)
        root.addLayout(grid)
        root.addWidget(self.lock_state)
        root.addWidget(self.warn)
        btns = QHBoxLayout()
        btns.addWidget(self.open_btn)
        btns.addWidget(self.lock_btn)
        btns.addStretch(1)
        root.addLayout(btns)

    def set_view(self, view: RaceView | None, now: datetime) -> None:
        self._view = view
        if view is None:
            self.title.setText("No upcoming thoroughbred race")
            self.meta.setText("Choose another date or wait for cached/live data.")
            self.primary.setText("—")
            self.backup.setText("—")
            self.confidence.setText("—")
            self.lock_state.setText("")
            self.warn.setText("")
            self.open_btn.setEnabled(False)
            self.lock_btn.setEnabled(False)
            return
        dist = f"{view.distance_m}m" if view.distance_m else "—"
        self.title.setText(f"{view.venue}  R{view.race_no}")
        self.meta.setText(
            f"{view.clock()}  ·  {view.countdown(now)}  ·  {dist}  ·  {view.race_class or '—'}  ·  {view.track_condition or '—'}"
        )
        self.primary.setText(format_runner_pick(view.primary_no, view.primary, view.odds))
        self.backup.setText(format_runner_pick(view.backup_no, view.backup, view.backup_odds))
        self.confidence.setText(view.confidence_label or "—")
        lock = "Saved snapshot" if view.from_snapshot else "Live model pick"
        if view.locked:
            lock += " · locked"
        self.lock_state.setText(lock)
        self.warn.setText("Scratching warning" if view.scratching_warning else "")
        self.open_btn.setEnabled(True)
        self.lock_btn.setEnabled(bool(view.primary) and not view.locked)

    def tick(self, now: datetime) -> None:
        if self._view is not None:
            dist = f"{self._view.distance_m}m" if self._view.distance_m else "—"
            self.meta.setText(
                f"{self._view.clock()}  ·  {self._view.countdown(now)}  ·  {dist}  ·  "
                f"{self._view.race_class or '—'}  ·  {self._view.track_condition or '—'}"
            )

    @property
    def view(self) -> RaceView | None:
        return self._view
