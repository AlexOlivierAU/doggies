"""In-app notification boundary. Native OS toasts are a follow-up."""

from __future__ import annotations

from typing import Callable, Optional

from services.result_service import BACKUP_WON, WIN
from services.race_day_service import minutes_until


class NotificationService:
    def __init__(self, already: Optional[set[str]] = None, persist: Optional[Callable[[str], None]] = None) -> None:
        self._seen: set[str] = set(already or ())
        self._persist = persist

    def _emit(self, ident: str, title: str, body: str) -> Optional[tuple[str, str]]:
        if ident in self._seen:
            return None
        self._seen.add(ident)
        if self._persist:
            self._persist(ident)
        return title, body

    def evaluate(self, *, views, picks_rows, now, enabled: bool) -> list[tuple[str, str]]:
        if not enabled:
            return []
        out: list[tuple[str, str]] = []
        for view in views or []:
            key = f"{getattr(view, 'meeting_url', '')}:{getattr(view, 'race_no', '')}"
            mins = minutes_until(getattr(view, "jump_at", None), now)
            if mins is not None and 0 < mins <= 5:
                msg = self._emit(
                    f"five:{key}",
                    "Five minutes to jump",
                    f"{view.venue} R{view.race_no} jumps soon.",
                )
                if msg:
                    out.append(msg)
            if getattr(view, "scratching_warning", False) or getattr(view, "primary_scratched", False):
                horse = getattr(view, "original_primary", "") or getattr(view, "primary", "")
                msg = self._emit(
                    f"scratch:{key}:{horse}",
                    "Primary scratched",
                    f"{view.venue} R{view.race_no}: {view.selection_warning or 'scratching warning'}.",
                )
                if msg:
                    out.append(msg)
        for row in picks_rows or []:
            status = str(row.get("result") or row.get("status") or "")
            ident = f"result:{row.get('meeting_url')}:{row.get('race_no')}:{status}"
            if status == WIN:
                msg = self._emit(ident, "Primary won", f"{row.get('venue')} R{row.get('race_no')}")
                if msg:
                    out.append(msg)
            elif status == BACKUP_WON:
                msg = self._emit(ident, "Backup won", f"{row.get('venue')} R{row.get('race_no')}")
                if msg:
                    out.append(msg)
            elif status in {WIN, BACKUP_WON} or status in {"WIN", "BACKUP WON"}:
                pass
            elif status and status not in {"PENDING", "AWAITING RESULT", ""}:
                msg = self._emit(ident, "Result confirmed", f"{row.get('venue')} R{row.get('race_no')}: {status}")
                if msg:
                    out.append(msg)
        return out
