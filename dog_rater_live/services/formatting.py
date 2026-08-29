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


def format_runner_pick(number=None, name: str = "", odds=None) -> str:
    """Present an official program number, horse name, and optional odds.

    Example: ``5. SARAH'S SONNETS · $4.80``
    Missing/invalid numbers are omitted rather than shown as 0/None/.
    """
    from services.runner_numbers import coerce_program_number

    label = (name or "").strip()
    if not label:
        return "—"
    display = label.upper()
    num = coerce_program_number(number)
    core = f"{num}. {display}" if num is not None else display
    if odds is None or odds == "":
        return core
    try:
        price = float(odds)
    except (TypeError, ValueError):
        return core
    if price <= 0:
        return core
    return f"{core} · ${price:.2f}"


def markdown_safe_pick(text: str) -> str:
    """Escape a leading ``N. `` so Streamlit markdown does not render an ordered list."""
    import re

    return re.sub(r"^(\d+)\. ", r"\1\\. ", text or "")


def format_saved_selection(pick: dict | None, which: str = "primary") -> str:
    """Format a stored primary/backup using snapshot numbers, never live card data."""
    from services.runner_numbers import saved_pick_number

    pick = pick or {}
    if which == "backup":
        name = str(pick.get("backup") or "")
        odds = pick.get("backup_odds")
    else:
        name = str(pick.get("original_primary") or pick.get("pick_name") or "")
        odds = pick.get("primary_odds")
    return format_runner_pick(number=saved_pick_number(pick, which), name=name, odds=odds)
