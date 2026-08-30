"""Native runner/pick details dialog. Uses already-loaded data only."""

from __future__ import annotations

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
)

from desktop.images.silk_cache import silk_cache
from desktop.table_theme import SILK_DIALOG
from desktop.themes.theme_manager import current


class RunnerDetailsDialog(QDialog):
    def __init__(self, detail: dict | None, parent=None) -> None:
        super().__init__(parent)
        detail = detail or {}
        role = str(detail.get("role") or "Runner").title()
        name = str(detail.get("name") or detail.get("raw_name") or "")
        no = detail.get("no") or detail.get("no_value") or ""
        self.setWindowTitle(f"{role} · {name}".strip(" ·"))
        self.setMinimumWidth(380)

        silk = QLabel()
        silk.setFixedSize(SILK_DIALOG + 8, SILK_DIALOG + 8)
        url = str(detail.get("silk") or detail.get("silk_url") or "")
        pm = silk_cache().pixmap(url) if url else None
        if pm is not None and not pm.isNull():
            silk.setPixmap(pm.scaled(SILK_DIALOG, SILK_DIALOG))
        else:
            silk.setText("No silk")
            silk.setStyleSheet(f"color: {current().chrome.hex('secondary')};")
            if url:
                silk_cache().request(url)

        title = QLabel(f"{str(no) + '. ' if no not in (None, '') else ''}{(name or '—').upper()}")
        title.setObjectName("heroTitle")
        title.setWordWrap(True)

        form = QFormLayout()
        def add(label: str, value) -> None:
            text = "—" if value in (None, "", []) else str(value)
            lab = QLabel(text)
            lab.setWordWrap(True)
            form.addRow(label, lab)

        add("Role", role)
        add("Barrier", detail.get("barrier") if detail.get("barrier") not in (None, "") else "—")
        odds = detail.get("odds") or detail.get("odds_value") or detail.get("win_odds")
        fluc = detail.get("fluc") or ""
        add("Odds", f"${float(odds):.2f}{fluc}" if isinstance(odds, (int, float)) else (str(odds) + fluc if odds else "—"))
        add("Form", detail.get("last10") or detail.get("form") or "—")
        add("Last class", detail.get("last_class") or "—")
        add("Today's class", detail.get("class_label") or "—")
        add("Class", detail.get("class_arrow") or "—")
        wt = detail.get("weight") or detail.get("weight_kg")
        add("Weight", f"{wt}kg" if isinstance(wt, (int, float)) else (wt or "—"))
        add("Jockey", detail.get("jockey") or "—")
        add("Trainer", detail.get("trainer") or "—")
        add("Score", detail.get("score") or "—")
        add("Scratched", "Yes" if detail.get("scratched") else "No")

        why = QTextEdit()
        why.setReadOnly(True)
        bullets = detail.get("why") or []
        kf = detail.get("key_factors") or ""
        lines = [kf] + [f"• {b}" for b in bullets]
        why.setPlainText("\n".join(x for x in lines if x) or "No ranking notes.")
        why.setMaximumHeight(120)

        source = QPushButton("Open source / form")
        src = str(detail.get("profile_url") or detail.get("race_url") or "")
        source.setEnabled(bool(src))
        source.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(src)))

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)

        head = QHBoxLayout()
        head.addWidget(silk)
        head.addWidget(title, 1)

        root = QVBoxLayout(self)
        root.addLayout(head)
        root.addLayout(form)
        root.addWidget(QLabel("Why"))
        root.addWidget(why)
        root.addWidget(source)
        root.addWidget(buttons)


def show_runner_details(detail: dict | None, parent=None) -> None:
    dlg = RunnerDetailsDialog(detail, parent)
    dlg.exec()
