"""Bottom status line helpers."""

from __future__ import annotations

from PySide6.QtWidgets import QLabel, QStatusBar


class StatusBarHost:
    def __init__(self, bar: QStatusBar) -> None:
        self.bar = bar
        self._label = QLabel("Ready")
        bar.addWidget(self._label, 1)

    def set_message(self, text: str) -> None:
        self._label.setText(text or "Ready")
        self.bar.showMessage(text or "Ready", 4000)
