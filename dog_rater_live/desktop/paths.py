"""Filesystem locations that must not depend on the process working directory."""

from __future__ import annotations

from pathlib import Path

from db_cache import default_db_path

PACKAGE_ROOT = Path(__file__).resolve().parent.parent


def desktop_log_path() -> Path:
    return PACKAGE_ROOT / "cache" / "desktop.log"


def desktop_log_dir() -> Path:
    return desktop_log_path().parent


def shared_default_db_path() -> Path:
    return default_db_path()
