"""Rounded status / confidence badges."""

from __future__ import annotations

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QPainter, QPainterPath
from PySide6.QtWidgets import QStyleOptionViewItem

from desktop.delegates.row_delegate import RowToneDelegate
from desktop.roles import CONFIDENCE_ROLE, RESULT_STATUS_ROLE
from desktop.table_theme import badge_colors
from desktop.themes.theme_manager import current


class BadgeDelegate(RowToneDelegate):
    def paint_cell(self, painter: QPainter, option: QStyleOptionViewItem, index) -> None:
        label = str(index.data(RESULT_STATUS_ROLE) or index.data(CONFIDENCE_ROLE) or index.data(0) or "").strip()
        if not label or label == "—":
            painter.setPen(current().table.muted)
            painter.drawText(option.rect.adjusted(6, 0, -4, 0), int(Qt.AlignmentFlag.AlignVCenter), "—")
            return
        bg, fg = badge_colors(label)
        # Keep chip contrast on selected/hovered rows — do not flatten onto selection blue.
        metrics = option.fontMetrics
        w = metrics.horizontalAdvance(label) + 14
        h = min(22, option.rect.height() - 8)
        x = option.rect.left() + 6
        y = option.rect.top() + (option.rect.height() - h) // 2
        rect = QRect(x, y, min(w, option.rect.width() - 10), h)
        path = QPainterPath()
        path.addRoundedRect(rect, 4, 4)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.fillPath(path, bg)
        painter.setPen(fg)
        painter.drawText(rect, int(Qt.AlignmentFlag.AlignCenter), label)
