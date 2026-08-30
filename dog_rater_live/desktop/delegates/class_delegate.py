"""Last class + Streamlit class arrows: ↑ up, ↓ down, → same."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QStyle, QStyleOptionViewItem

from desktop.delegates.row_delegate import RowToneDelegate
from desktop.roles import CLASS_ARROW_ROLE, CLASS_LABEL_ROLE, LAST_CLASS_ROLE
from desktop.table_theme import class_arrow_color, class_arrow_tip
from desktop.themes.theme_manager import current


def class_payload(index) -> dict:
    last = str(index.data(LAST_CLASS_ROLE) or "")
    today = str(index.data(CLASS_LABEL_ROLE) or "")
    arrow = str(index.data(CLASS_ARROW_ROLE) or "")
    parts = [p for p in (last, arrow, today) if p]
    display = " ".join(parts) if parts else "—"
    return {"last": last, "today": today, "arrow": arrow, "display": display, "tip": class_arrow_tip(arrow)}


class ClassDelegate(RowToneDelegate):
    def paint_cell(self, painter: QPainter, option: QStyleOptionViewItem, index) -> None:
        payload = class_payload(index)
        theme = current()
        if option.state & QStyle.StateFlag.State_Selected:
            painter.setPen(theme.table.selection_text)
        elif payload["arrow"]:
            painter.setPen(class_arrow_color(payload["arrow"]))
        else:
            painter.setPen(theme.table.text)
        painter.drawText(
            option.rect.adjusted(6, 0, -4, 0),
            int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
            payload["display"],
        )
