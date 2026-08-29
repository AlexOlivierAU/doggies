"""Pure presentation helpers (no Streamlit)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional


def ordinal(n: Optional[int]) -> str:
    if n is None:
        return "—"
    try:
        v = int(n)
    except (TypeError, ValueError):
        return "—"
    if v <= 0:
        return "unplaced"
    if v % 100 in (11, 12, 13):
        return f"{v}th"
    suffix = {1: "st", 2: "nd", 3: "rd"}.get(v % 10, "th")
    return f"{v}{suffix}"


def format_countdown(jump_at: Optional[datetime], now: Optional[datetime]) -> str:
    if jump_at is None or now is None:
        return "—"
    delta = jump_at - now
    secs = int(delta.total_seconds())
    if secs <= -90:
        return "Jumped"
    if secs <= 0:
        return "Jumping"
    mins, rem = divmod(secs, 60)
    hours, mins = divmod(mins, 60)
    if hours > 0:
        return f"{hours}h {mins}m"
    if mins > 0:
        return f"{mins}m"
    return f"{rem}s"


def format_clock(dt: Optional[datetime]) -> str:
    if dt is None:
        return "—"
    return dt.strftime("%H:%M")


def minutes_until(jump_at: Optional[datetime], now: Optional[datetime]) -> Optional[float]:
    if jump_at is None or now is None:
        return None
    return (jump_at - now).total_seconds() / 60.0


def tb_race_duration() -> timedelta:
    return timedelta(minutes=35)
