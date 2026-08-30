"""Odds plus Streamlit fluctuation arrows: ↓ shorten, ↑ drift, → steady."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QStyle, QStyleOptionViewItem

from desktop.delegates.row_delegate import RowToneDelegate
from desktop.roles import FLUCTUATION_HISTORY_ROLE, FLUCTUATION_ROLE, ODDS_ROLE
from desktop.table_theme import fluc_color, fluc_tip
from desktop.themes.theme_manager import current


def odds_payload(index) -> dict[str, Any]:
    odds = index.data(ODDS_ROLE)
    fluc = str(index.data(FLUCTUATION_ROLE) or "")
    history = index.data(FLUCTUATION_HISTORY_ROLE) or []
    if odds in (None, "", "—"):
        text = "—"
    else:
        try:
            text = f"${float(odds):.2f}"
        except (TypeError, ValueError):
            text = "—"
    if fluc and text != "—":
        text = f"{text} {fluc}"
    return {"odds": odds, "fluc": fluc, "history": list(history or []), "display": text, "tip": fluc_tip(fluc)}


class OddsDelegate(RowToneDelegate):
    def paint_cell(self, painter: QPainter, option: QStyleOptionViewItem, index) -> None:
        payload = odds_payload(index)
        theme = current()
        if option.state & QStyle.StateFlag.State_Selected:
            painter.setPen(theme.table.selection_text)
        elif payload["fluc"]:
            painter.setPen(fluc_color(payload["fluc"]))
        else:
            painter.setPen(theme.table.text)
        painter.drawText(option.rect.adjusted(6, 0, -4, 0), int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), payload["display"])
