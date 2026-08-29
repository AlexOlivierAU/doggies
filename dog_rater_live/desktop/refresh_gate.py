"""Coalesce overlapping refresh requests (no Qt)."""

from __future__ import annotations

from typing import Optional

_PRIORITY = {"card": 3, "all": 2, "odds": 1, "results": 1}


def merge_kinds(a: Optional[str], b: Optional[str]) -> Optional[str]:
    if not a:
        return b
    if not b:
        return a
    if a == b:
        return a
    if "card" in (a, b):
        return "card"
    if {a, b} == {"odds", "results"} or "all" in (a, b):
        return "all"
    return b if _PRIORITY.get(b, 0) >= _PRIORITY.get(a, 0) else a


class RefreshGate:
    """If a job is running, remember the next kind instead of starting another."""

    def __init__(self) -> None:
        self.busy = False
        self.pending: Optional[str] = None

    def request(self, kind: str) -> bool:
        """True if the caller should start work now."""
        if self.busy:
            self.pending = merge_kinds(self.pending, kind)
            return False
        self.busy = True
        self.pending = None
        return True

    def finish(self) -> Optional[str]:
        """Clear busy. Return a coalesced follow-up kind, if any."""
        self.busy = False
        pending = self.pending
        self.pending = None
        return pending
