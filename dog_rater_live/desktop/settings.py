"""QSettings-backed desktop preferences."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import QByteArray, QSettings

from desktop import APP_NAME, ORG_NAME
from race_db import _DEFAULT_DB

STATES = ("All", "NSW", "VIC", "QLD", "SA", "WA", "TAS")
TIMEZONES = ("Australia/Sydney", "Australia/Brisbane", "Australia/Adelaide", "Australia/Perth", "Pacific/Auckland", "Local (server)")


class DesktopSettings:
    def __init__(self, settings: QSettings | None = None) -> None:
        self._s = settings or QSettings(ORG_NAME, APP_NAME)

    def _get(self, key: str, default: Any, cast=None):
        val = self._s.value(key, default)
        if cast is bool:
            if isinstance(val, bool):
                return val
            return str(val).lower() in {"1", "true", "yes"}
        if cast is int:
            try:
                return int(val)
            except (TypeError, ValueError):
                return int(default)
        if cast is str:
            return str(val if val is not None else default)
        return val if val is not None else default

    @property
    def timezone(self) -> str:
        return self._get("timezone", "Australia/Sydney", str)

    @timezone.setter
    def timezone(self, value: str) -> None:
        self._s.setValue("timezone", value)

    @property
    def state_filter(self) -> str:
        v = self._get("state_filter", "All", str)
        return v if v in STATES else "All"

    @state_filter.setter
    def state_filter(self, value: str) -> None:
        self._s.setValue("state_filter", value)

    @property
    def auto_refresh(self) -> bool:
        return self._get("auto_refresh", True, bool)

    @auto_refresh.setter
    def auto_refresh(self, value: bool) -> None:
        self._s.setValue("auto_refresh", bool(value))

    @property
    def interval_odds_sec(self) -> int:
        return max(15, min(180, self._get("interval_odds_sec", 45, int)))

    @interval_odds_sec.setter
    def interval_odds_sec(self, value: int) -> None:
        self._s.setValue("interval_odds_sec", int(value))

    @property
    def interval_fields_sec(self) -> int:
        return max(60, min(600, self._get("interval_fields_sec", 180, int)))

    @interval_fields_sec.setter
    def interval_fields_sec(self, value: int) -> None:
        self._s.setValue("interval_fields_sec", int(value))

    @property
    def interval_results_sec(self) -> int:
        return max(15, min(180, self._get("interval_results_sec", 30, int)))

    @interval_results_sec.setter
    def interval_results_sec(self, value: int) -> None:
        self._s.setValue("interval_results_sec", int(value))

    @property
    def notifications(self) -> bool:
        return self._get("notifications", True, bool)

    @notifications.setter
    def notifications(self, value: bool) -> None:
        self._s.setValue("notifications", bool(value))

    @property
    def theme(self) -> str:
        return self._get("theme", "dark", str)

    @theme.setter
    def theme(self, value: str) -> None:
        self._s.setValue("theme", value)

    @property
    def db_path(self) -> Path:
        raw = self._get("db_path", str(_DEFAULT_DB), str)
        return Path(raw).expanduser()

    @db_path.setter
    def db_path(self, value: Path | str) -> None:
        self._s.setValue("db_path", str(value))

    @property
    def other_codes_enabled(self) -> bool:
        return self._get("other_codes_enabled", False, bool)

    @other_codes_enabled.setter
    def other_codes_enabled(self, value: bool) -> None:
        self._s.setValue("other_codes_enabled", bool(value))

    @property
    def last_page(self) -> str:
        return self._get("last_page", "Race Day", str)

    @last_page.setter
    def last_page(self, value: str) -> None:
        self._s.setValue("last_page", value)

    def geometry(self) -> QByteArray:
        val = self._s.value("geometry")
        return val if isinstance(val, QByteArray) else QByteArray()

    def set_geometry(self, data: QByteArray) -> None:
        self._s.setValue("geometry", data)

    def column_widths(self, table: str) -> list[int]:
        raw = self._s.value(f"columns/{table}", [])
        if not raw:
            return []
        if isinstance(raw, list):
            out = []
            for x in raw:
                try:
                    out.append(int(x))
                except (TypeError, ValueError):
                    continue
            return out
        return []

    def set_column_widths(self, table: str, widths: list[int]) -> None:
        self._s.setValue(f"columns/{table}", [int(w) for w in widths])

    def notified_ids(self) -> set[str]:
        raw = self._s.value("notified_ids", [])
        if isinstance(raw, str):
            return {p for p in raw.split("|") if p}
        if isinstance(raw, list):
            return {str(x) for x in raw if x}
        return set()

    def add_notified(self, ident: str) -> None:
        ids = self.notified_ids()
        ids.add(ident)
        # Keep the set from growing without bound.
        keep = sorted(ids)[-400:]
        self._s.setValue("notified_ids", keep)

    def sync(self) -> None:
        self._s.sync()
