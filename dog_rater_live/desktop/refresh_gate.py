"""Coalesce overlapping refresh requests (no Qt)."""

from __future__ import annotations

from typing import Optional

# Higher number runs first when several jobs are queued.
_PRIORITY = {"cached": 4, "card": 3, "all": 2, "odds": 2, "results": 1}


def merge_kinds(a: Optional[str], b: Optional[str]) -> Optional[str]:
    if not a:
        return b
    if not b:
        return a
    if a == b:
        return a
    if "cached" in (a, b) and "card" in (a, b):
        return "card"
    if "card" in (a, b):
        return "card"
    if {a, b} == {"odds", "results"} or "all" in (a, b):
        return "all"
    return b if _PRIORITY.get(b, 0) >= _PRIORITY.get(a, 0) else a


class RefreshGate:
    """If a job is running, remember follow-up kinds instead of starting another.

    Pending jobs are kept as a set so coalescing never drops a required follow-up
    (e.g. results queued while odds is already pending).
    """

    def __init__(self) -> None:
        self.busy = False
        self.pending: set[str] = set()

    def request(self, kind: str) -> bool:
        """True if the caller should start work now."""
        if self.busy:
            self.pending.add(kind)
            return False
        self.busy = True
        self.pending.discard(kind)
        return True

    def finish(self) -> Optional[str]:
        """Clear busy. Return the highest-priority follow-up kind, if any."""
        self.busy = False
        if not self.pending:
            return None
        kind = max(self.pending, key=lambda k: _PRIORITY.get(k, 0))
        self.pending.discard(kind)
        return kind

    def has_pending(self, kind: str) -> bool:
        return kind in self.pending
