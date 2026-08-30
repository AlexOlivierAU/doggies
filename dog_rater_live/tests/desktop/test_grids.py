from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap

from desktop.demo_fixture import NOW, demo_upcoming_views
from desktop.images.silk_cache import reset_silk_cache, silk_cache
from desktop.models.race_table_model import RaceTableModel, race_to_row
from desktop.roles import HORSE_NAME_ROLE, PROGRAM_NUMBER_ROLE, RACE_KEY_ROLE, SORT_ROLE
from desktop.widgets.styled_table import StyledTableView


def test_silk_cache_inject_does_not_network(qapp):
    reset_silk_cache()
    cache = silk_cache()
    cache._network_enabled = False
    cache._nam = None
    url = "https://example.test/silk.png"
    pm = QPixmap(16, 16)
    pm.fill()
    cache.inject(url, pm)
    assert cache.pixmap(url) is not None
    assert cache._get_count == 0


def test_sort_and_selection_and_widths(qapp):
    views = demo_upcoming_views()
    model = RaceTableModel()
    model.set_rows([race_to_row(v, NOW) for v in views])
    table = StyledTableView(sorting=True, name="upcoming")
    table.set_source_model(model)
    table.resize(800, 240)
    table.show()
    qapp.processEvents()

    src = model.index(0, 4)
    assert model.data(src, PROGRAM_NUMBER_ROLE) not in (None, "")
    assert model.data(src, HORSE_NAME_ROLE)
    assert model.data(model.index(0, 0), RACE_KEY_ROLE) == views[0].race_key

    key = views[1].race_key
    table.restore_selection(key)
    assert table.selected_key() == key

    table.apply_column_widths([80, 90, 140, 50, 220])
    assert table.columnWidth(0) == 80
    table.apply_column_widths([40, 40, 40])
    assert table.columnWidth(0) == 80

    table.sortByColumn(0, Qt.SortOrder.DescendingOrder)
    qapp.processEvents()
    assert table.selected_key() == key
    jump_sort = [model.data(model.index(i, 0), SORT_ROLE) for i in range(model.rowCount())]
    assert jump_sort == sorted(jump_sort)


def test_program_number_role_is_not_barrier(qapp):
    view = demo_upcoming_views()[0]
    row = race_to_row(view, NOW)
    assert row["primary_no"] == "5"
    assert row["primary_barrier"] == 12
    model = RaceTableModel()
    model.set_rows([row])
    assert model.data(model.index(0, 4), PROGRAM_NUMBER_ROLE) == "5"
    from desktop.roles import BARRIER_ROLE

    assert model.data(model.index(0, 4), BARRIER_ROLE) == 12
