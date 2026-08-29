"""Launch Race Day Rater (PySide6). Importing this module does not start the event loop."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from desktop import APP_NAME, ORG_NAME

log = logging.getLogger("race_day_rater")


def _load_qss(app) -> None:
    qss = Path(__file__).resolve().parent / "resources" / "styles.qss"
    try:
        app.setStyleSheet(qss.read_text(encoding="utf-8"))
    except Exception:
        log.exception("Could not load stylesheet")


def _configure_logging() -> None:
    if logging.getLogger().handlers:
        return
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        log_path = Path(__file__).resolve().parent.parent / "cache" / "desktop.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_path, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logging.getLogger().addHandler(fh)
    except Exception:
        pass


def create_app(argv=None):
    """Create QApplication + MainWindow without exec(). Safe for tests."""
    from PySide6.QtCore import QCoreApplication
    from PySide6.QtWidgets import QApplication

    QCoreApplication.setOrganizationName(ORG_NAME)
    QCoreApplication.setApplicationName(APP_NAME)
    _configure_logging()
    app = QApplication.instance()
    created = False
    if app is None:
        app = QApplication(list(argv) if argv is not None else sys.argv)
        created = True
        app.setApplicationDisplayName(APP_NAME)
        _load_qss(app)
    from desktop.application_controller import ApplicationController
    from desktop.main_window import MainWindow
    from desktop.settings import DesktopSettings

    controller = ApplicationController(DesktopSettings())
    window = MainWindow(controller)
    if created:
        app.aboutToQuit.connect(controller.shutdown)
    return app, window


def main(argv=None) -> int:
    app, window = create_app(argv)
    window.show()
    window.controller.start()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
