"""Subtle row fill and left accent from ROW_TONE_ROLE."""

from __future__ import annotations

from PySide6.QtCore import QRect
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QStyledItemDelegate, QStyle, QStyleOptionViewItem

from desktop.roles import ROW_TONE_ROLE
from desktop.table_theme import accent_for_tone, fill_for_tone
from desktop.themes.theme_manager import current


class RowToneDelegate(QStyledItemDelegate):
    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index) -> None:
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        painter.save()
        tone = str(index.data(ROW_TONE_ROLE) or "")
        selected = bool(opt.state & QStyle.StateFlag.State_Selected)
        theme = current()
        if selected:
            if opt.state & QStyle.StateFlag.State_Active:
                painter.fillRect(opt.rect, theme.table.selection)
            else:
                painter.fillRect(opt.rect, theme.table.selection_inactive)
        else:
            fill = fill_for_tone(tone)
            if fill.alpha() > 0:
                painter.fillRect(opt.rect, fill)
            elif opt.state & QStyle.StateFlag.State_MouseOver:
                painter.fillRect(opt.rect, theme.table.hover)
            elif opt.features & QStyleOptionViewItem.ViewItemFeature.Alternate:
                painter.fillRect(opt.rect, theme.table.alternate_row)
            else:
                painter.fillRect(opt.rect, theme.table.background)
        accent = accent_for_tone(tone)
        if accent.alpha() > 0:
            painter.fillRect(QRect(opt.rect.left(), opt.rect.top(), 3, opt.rect.height()), accent)
        painter.restore()
        self.paint_cell(painter, opt, index)

    def paint_cell(self, painter: QPainter, option: QStyleOptionViewItem, index) -> None:
        opt = QStyleOptionViewItem(option)
        opt.backgroundBrush = QColor(0, 0, 0, 0)
        enabled = bool(opt.state & QStyle.StateFlag.State_Enabled)
        if opt.state & QStyle.StateFlag.State_Selected:
            opt.palette.setColor(opt.palette.ColorRole.HighlightedText, current().table.selection_text)
            opt.palette.setColor(opt.palette.ColorRole.Text, current().table.selection_text)
        elif not enabled:
            opt.palette.setColor(opt.palette.ColorRole.Text, current().table.disabled)
        else:
            opt.palette.setColor(opt.palette.ColorRole.Text, current().table.text)
        opt.state &= ~QStyle.StateFlag.State_Selected
        super().paint(painter, opt, index)
