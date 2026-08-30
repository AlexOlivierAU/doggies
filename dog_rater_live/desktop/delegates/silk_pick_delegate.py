"""Primary/backup horse cell and silk thumbnail. Paint never downloads."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QRect, QSize, Qt
from PySide6.QtGui import QFont, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QStyleOptionViewItem

from desktop.delegates.row_delegate import RowToneDelegate
from desktop.images.silk_cache import silk_cache
from desktop.roles import (
    BARRIER_ROLE,
    COMPACT_PICK_ROLE,
    FLUCTUATION_ROLE,
    HORSE_NAME_ROLE,
    ODDS_ROLE,
    PICK_ROLE,
    PROGRAM_NUMBER_ROLE,
    SCRATCHED_ROLE,
    SILK_URL_ROLE,
)
from desktop.table_theme import SILK_LOGICAL
from desktop.themes.theme_manager import current


def pick_payload(index) -> dict[str, Any]:
    number = index.data(PROGRAM_NUMBER_ROLE)
    name = str(index.data(HORSE_NAME_ROLE) or "").strip()
    scratched = bool(index.data(SCRATCHED_ROLE))
    odds = index.data(ODDS_ROLE)
    fluc = str(index.data(FLUCTUATION_ROLE) or "")
    barrier = index.data(BARRIER_ROLE)
    display = format_pick_text(
        number=number,
        name=name,
        odds=odds,
        fluc=fluc,
        barrier=barrier,
        scratched=scratched,
        compact=bool(index.data(COMPACT_PICK_ROLE)),
    )
    return {
        "silk_url": str(index.data(SILK_URL_ROLE) or ""),
        "number": number,
        "name": name,
        "barrier": barrier,
        "odds": odds,
        "fluc": fluc,
        "role": str(index.data(PICK_ROLE) or ""),
        "scratched": scratched,
        "display": display,
        "tooltip": display,
    }


def format_pick_text(
    *,
    number=None,
    name: str = "",
    odds=None,
    fluc: str = "",
    barrier=None,
    scratched: bool = False,
    compact: bool = True,
) -> str:
    label = (name or "").strip()
    if scratched and label:
        label = f"{label} SCR"
    elif scratched:
        label = "SCR"
    if not label:
        return "—"
    display = label.upper()
    try:
        num = int(number) if number not in (None, "", "—") else None
        if num is not None and num <= 0:
            num = None
    except (TypeError, ValueError):
        num = None
    core = f"{num}. {display}" if num is not None else display
    bits = [core]
    if not compact and barrier not in (None, "", "—"):
        bits.append(f"(barrier {barrier})")
    if odds not in (None, "", "—"):
        try:
            price = float(odds)
            if price > 0:
                bits.append(f"${price:.2f}{fluc or ''}")
        except (TypeError, ValueError):
            pass
    return " · ".join(bits) if compact else " ".join(bits)


def _scaled_silk(pm: QPixmap, logical: int, dpr: float) -> QPixmap:
    target = max(8, int(logical * dpr))
    scaled = pm.scaled(target, target, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
    scaled.setDevicePixelRatio(dpr)
    return scaled


class SilkDelegate(RowToneDelegate):
    def paint_cell(self, painter: QPainter, option: QStyleOptionViewItem, index) -> None:
        url = str(index.data(SILK_URL_ROLE) or "")
        cache = silk_cache()
        pm = cache.pixmap(url) if url else None
        dpr = painter.device().devicePixelRatioF() if painter.device() else 1.0
        box = QRect(option.rect.left() + 6, option.rect.top() + 3, SILK_LOGICAL, option.rect.height() - 6)
        if pm is not None and not pm.isNull():
            scaled = _scaled_silk(pm, SILK_LOGICAL, dpr)
            x = box.left() + (box.width() - int(scaled.width() / dpr)) // 2
            y = box.top() + (box.height() - int(scaled.height() / dpr)) // 2
            painter.drawPixmap(x, y, scaled)
        else:
            painter.setPen(QPen(current().chrome.border))
            painter.setBrush(current().chrome.placeholder)
            painter.drawRoundedRect(box.adjusted(0, 2, -4, -2), 3, 3)

    def sizeHint(self, option, index) -> QSize:
        return QSize(SILK_LOGICAL + 12, option.rect.height() or 32)


class PickDelegate(RowToneDelegate):
    def __init__(self, parent=None, *, compact: bool = True, show_silk: bool = True) -> None:
        super().__init__(parent)
        self.compact = compact
        self.show_silk = show_silk

    def paint_cell(self, painter: QPainter, option: QStyleOptionViewItem, index) -> None:
        from PySide6.QtWidgets import QStyle

        payload = pick_payload(index)
        theme = current()
        dpr = painter.device().devicePixelRatioF() if painter.device() else 1.0
        x = option.rect.left() + 8
        y = option.rect.top()
        h = option.rect.height()
        if self.show_silk:
            url = payload["silk_url"]
            cache = silk_cache()
            pm = cache.pixmap(url) if url else None
            box = QRect(x, y + 3, SILK_LOGICAL, h - 6)
            if pm is not None and not pm.isNull():
                scaled = _scaled_silk(pm, SILK_LOGICAL, dpr)
                painter.drawPixmap(x, y + max(2, (h - int(scaled.height() / dpr)) // 2), scaled)
            else:
                painter.setPen(QPen(theme.chrome.border))
                painter.setBrush(theme.chrome.placeholder)
                painter.drawRoundedRect(box.adjusted(0, 2, -2, -2), 3, 3)
            x += SILK_LOGICAL + 8
        text_rect = QRect(x, y, option.rect.right() - x - 4, h)
        if option.state & QStyle.StateFlag.State_Selected:
            painter.setPen(theme.table.selection_text)
        elif payload["scratched"]:
            painter.setPen(theme.semantic.scratch)
        elif payload["role"] == "primary":
            painter.setPen(theme.pick.primary)
        elif payload["role"] == "backup":
            painter.setPen(theme.pick.backup)
        else:
            painter.setPen(theme.table.text)
        font = QFont(option.font)
        if payload["scratched"]:
            font.setStrikeOut(True)
        painter.setFont(font)
        painter.drawText(
            text_rect,
            int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
            payload["display"],
        )

    def sizeHint(self, option, index) -> QSize:
        return QSize(180, option.rect.height() or 32)
