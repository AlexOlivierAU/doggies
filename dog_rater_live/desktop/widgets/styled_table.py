"""QTableView wrapper: proxy sort, silk refresh, selection by race key."""

from __future__ import annotations

from PySide6.QtCore import QModelIndex, QSortFilterProxyModel, Qt, Signal
from PySide6.QtWidgets import QTableView

from desktop.delegates.badge_delegate import BadgeDelegate
from desktop.delegates.class_delegate import ClassDelegate
from desktop.delegates.odds_delegate import OddsDelegate
from desktop.delegates.row_delegate import RowToneDelegate
from desktop.delegates.silk_pick_delegate import PickDelegate, SilkDelegate
from desktop.images.silk_cache import silk_cache
from desktop.roles import RACE_KEY_ROLE, SORT_ROLE
from desktop.table_theme import configure_table


class RoleSortProxy(QSortFilterProxyModel):
    def lessThan(self, left: QModelIndex, right: QModelIndex) -> bool:
        lv = self.sourceModel().data(left, SORT_ROLE)
        rv = self.sourceModel().data(right, SORT_ROLE)
        if lv is None:
            lv = self.sourceModel().data(left, Qt.ItemDataRole.DisplayRole)
        if rv is None:
            rv = self.sourceModel().data(right, Qt.ItemDataRole.DisplayRole)
        try:
            return lv < rv
        except TypeError:
            return str(lv) < str(rv)


class StyledTableView(QTableView):
    row_activated = Signal(object)
    context_row = Signal(object, object)

    def __init__(self, parent=None, *, sorting: bool = True, name: str = "") -> None:
        super().__init__(parent)
        self.table_name = name
        self._source = None
        self.proxy = RoleSortProxy(self)
        self.proxy.setSortRole(SORT_ROLE)
        super().setModel(self.proxy)
        configure_table(self, sorting=sorting)
        self.setItemDelegate(RowToneDelegate(self))
        self.doubleClicked.connect(self._emit_activated)
        self.activated.connect(self._emit_activated)
        self.customContextMenuRequested.connect(self._emit_context)
        self._silk_connected = False
        self._widths_applied = False

    def set_source_model(self, model) -> None:
        self._source = model
        self.proxy.setSourceModel(model)
        cache = silk_cache()
        if not self._silk_connected:
            cache.silk_ready.connect(self._silk_arrived)
            self._silk_connected = True
        self.prefetch_silks()

    def source_model(self):
        return self._source

    def source_index(self, index: QModelIndex) -> QModelIndex:
        if not index.isValid():
            return QModelIndex()
        if index.model() is self.proxy:
            return self.proxy.mapToSource(index)
        return index

    def source_row(self, index: QModelIndex):
        src = self.source_index(index)
        model = self._source
        if model is None or not src.isValid():
            return None
        return model.row_at(src.row())

    def selected_key(self):
        idx = self.currentIndex()
        src = self.source_index(idx)
        if not src.isValid() or self._source is None:
            return None
        return self._source.data(self._source.index(src.row(), 0), RACE_KEY_ROLE)

    def restore_selection(self, race_key) -> None:
        if race_key is None or self._source is None or not hasattr(self._source, "find_row"):
            return
        row = self._source.find_row(race_key)
        if row < 0:
            return
        src = self._source.index(row, 0)
        proxy_idx = self.proxy.mapFromSource(src)
        if proxy_idx.isValid():
            self.selectRow(proxy_idx.row())
            self.scrollTo(proxy_idx)

    def apply_column_widths(self, widths: list[int]) -> None:
        if self._widths_applied or not widths:
            return
        for i, w in enumerate(widths):
            if w > 20:
                self.setColumnWidth(i, w)
        self._widths_applied = True

    def current_column_widths(self) -> list[int]:
        return [self.columnWidth(i) for i in range(self.model().columnCount())]

    def set_pick_columns(self, columns: list[int], *, compact: bool = True) -> None:
        for col in columns:
            self.setItemDelegateForColumn(col, PickDelegate(self, compact=compact, show_silk=True))

    def set_silk_column(self, col: int) -> None:
        self.setItemDelegateForColumn(col, SilkDelegate(self))

    def set_odds_columns(self, columns: list[int]) -> None:
        for col in columns:
            self.setItemDelegateForColumn(col, OddsDelegate(self))

    def set_badge_columns(self, columns: list[int]) -> None:
        for col in columns:
            self.setItemDelegateForColumn(col, BadgeDelegate(self))

    def set_class_columns(self, columns: list[int]) -> None:
        for col in columns:
            self.setItemDelegateForColumn(col, ClassDelegate(self))

    def prefetch_silks(self) -> None:
        model = self._source
        if model is None or not hasattr(model, "silk_urls"):
            return
        silk_cache().prefetch(model.silk_urls())

    def _silk_arrived(self, url: str) -> None:
        model = self._source
        if model is None or not hasattr(model, "indexes_for_silk"):
            return
        for src in model.indexes_for_silk(url):
            proxy_idx = self.proxy.mapFromSource(src)
            if proxy_idx.isValid():
                self.update(proxy_idx)

    def _emit_activated(self, index) -> None:
        self.row_activated.emit(self.source_row(index))

    def _emit_context(self, pos) -> None:
        index = self.indexAt(pos)
        self.context_row.emit(self.source_row(index), self.mapToGlobal(pos))
