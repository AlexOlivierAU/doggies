"""Launch Race Day Rater (PySide6). Importing this module does not start the event loop."""

from __future__ import annotations

import logging
import sys

from desktop import APP_NAME, ORG_NAME
from desktop.paths import desktop_log_path, shared_default_db_path
from desktop.themes.theme_manager import apply_to_application

log = logging.getLogger("race_day_rater")


def _load_qss(app, theme_id: str | None = None) -> None:
    try:
        apply_to_application(app, theme_id)
    except Exception:
        log.exception("Could not apply theme stylesheet")


def _configure_logging() -> None:
    if logging.getLogger().handlers:
        return
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        log_path = desktop_log_path()
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
    from desktop.application_controller import ApplicationController
    from desktop.main_window import MainWindow
    from desktop.settings import DesktopSettings

    settings = DesktopSettings()
    _load_qss(app, settings.theme)
    controller = ApplicationController(settings)
    window = MainWindow(controller)
    log.info("Database: %s", shared_default_db_path())
    log.info("Resolved database: %s exists=%s", controller.settings.db_path, controller.settings.db_path.exists())
    if controller.settings.db_path_warning:
        log.warning("%s", controller.settings.db_path_warning)
    if created:
        app.aboutToQuit.connect(controller.shutdown)
    return app, window


def main(argv=None) -> int:
    argv = list(argv) if argv is not None else sys.argv
    demo = "--demo-grids" in argv
    argv = [a for a in argv if a != "--demo-grids"]
    app, window = create_app(argv)
    if demo:
        from desktop.demo_fixture import load_demo_grids

        load_demo_grids(window)
    window.show()
    if not demo:
        window.controller.start()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
