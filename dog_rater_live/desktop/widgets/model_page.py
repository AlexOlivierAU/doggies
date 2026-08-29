"""Model lab is still hosted in Streamlit for this MVP."""

from __future__ import annotations

from PySide6.QtWidgets import QLabel, QTextEdit, QVBoxLayout, QWidget


class ModelPage(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        title = QLabel("Model")
        title.setObjectName("heroTitle")
        body = QTextEdit()
        body.setReadOnly(True)
        body.setPlainText(
            "Scoring is adaptive heuristic weighting (form, draw, class/weight, conditions).\n"
            "It is not a trained machine-learning model and it is not betting advice.\n\n"
            "Weight sliders and the compression backtest remain in the Streamlit app for this desktop MVP:\n\n"
            "    streamlit run app.py\n\n"
            "Open the Model section there. Race Day, Race Details, History and Settings are fully available in this window.\n"
        )
        root = QVBoxLayout(self)
        root.addWidget(title)
        root.addWidget(body)
