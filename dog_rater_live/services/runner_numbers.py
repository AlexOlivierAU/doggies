"""Official thoroughbred program/saddle numbers — never barriers."""

from __future__ import annotations

import re
from typing import Any, Optional

from services.names import names_match

_NO_HEADERS = {
    "no",
    "no.",
    "number",
    "saddle",
    "program",
    "#",
    "num",
    "runner no",
    "runner no.",
    "cloth",
    "saddlecloth",
    "saddle cloth",
}

# 5 / 5. / 5e / 5E / 5 (12)
_CELL_RE = re.compile(
    r"""
    ^\s*
    (\d{1,2})
    [eE]?
    \s*[.)]?
    (?:
        \s*\(\s*\d{1,2}\s*\)
    )?
    \s*$
    """,
    re.VERBOSE,
)

# "5. SARAH'S SONNETS" or "5e HORSE" in a horse cell
_LEADING_NAME_RE = re.compile(r"^\s*(\d{1,2})[eE]?[.)]?\s+([A-Za-z].+)$")


def coerce_program_number(value: Any) -> Optional[int]:
    """Return a positive program number, or None. Never 0 / None / 'None'."""
    if value is None or value is False:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float):
        if value != value or value <= 0:  # noqa: PLR0124  # NaN
            return None
        n = int(value)
        return n if n > 0 and float(n) == float(value) else None
    s = str(value).strip()
    if not s or s.lower() in {"none", "null", "—", "-", "n/a"}:
        return None
    parsed = parse_program_number_cell(s)
    return parsed


def parse_program_number_cell(text: str) -> Optional[int]:
    """Parse an official No/program cell from Racing Australia-style text."""
    s = (text or "").strip()
    if not s:
        return None
    m = _CELL_RE.match(s)
    if m:
        n = int(m.group(1))
        return n if n > 0 else None
    m = _LEADING_NAME_RE.match(s)
    if m:
        n = int(m.group(1))
        return n if n > 0 else None
    return None


def program_number_from_raw(headers: list, cells: list) -> Optional[int]:
    hn = [str(h).strip().lower() for h in (headers or [])]
    idx = next((i for i, h in enumerate(hn) if h in _NO_HEADERS), None)
    if idx is not None and 0 <= idx < len(cells or []):
        n = parse_program_number_cell(str(cells[idx]))
        if n is not None:
            return n
    # Unlabelled first column that is only a program number (not a barrier column).
    if hn and hn[0] in {"", " "} and cells:
        n = parse_program_number_cell(str(cells[0]))
        if n is not None:
            return n
    # Horse cell sometimes includes the cloth number as a prefix.
    horse_idx = next((i for i, h in enumerate(hn) if h in {"horse", "runner", "name"}), None)
    if horse_idx is not None and 0 <= horse_idx < len(cells or []):
        n = parse_program_number_cell(str(cells[horse_idx]))
        if n is not None:
            return n
    return None


def program_number_for_runner(runner: Any) -> Optional[int]:
    """Prefer explicit field, then raw No column. Do not use barrier/draw."""
    n = coerce_program_number(getattr(runner, "program_number", None))
    if n is not None:
        return n
    raw = getattr(runner, "raw", None) or {}
    if isinstance(raw, dict):
        n = coerce_program_number(raw.get("program_number"))
        if n is not None:
            return n
        n = program_number_from_raw(raw.get("headers") or [], raw.get("cells") or [])
        if n is not None:
            return n
    return None


def program_number_for_name(runners: list, name: str) -> Optional[int]:
    if not name:
        return None
    for r in runners or []:
        if getattr(r, "name", None) == name:
            return program_number_for_runner(r)
        if names_match(str(getattr(r, "name", "") or ""), name):
            return program_number_for_runner(r)
    return None


def number_from_field_snapshot(field: list | None, name: str) -> Optional[int]:
    if not name:
        return None
    for row in field or []:
        if not isinstance(row, dict):
            continue
        row_name = str(row.get("name") or "")
        if row_name == name or names_match(row_name, name):
            n = coerce_program_number(row.get("program_number") or row.get("number"))
            if n is not None:
                return n
    return None


def saved_pick_number(pick: dict[str, Any] | None, which: str = "primary") -> Optional[int]:
    """Official number from a saved snapshot only — never live card data."""
    pick = pick or {}
    key = "primary_number" if which == "primary" else "backup_number"
    n = coerce_program_number(pick.get(key))
    if n is not None:
        return n
    snap = pick.get("snapshot") or {}
    if isinstance(snap, dict):
        n = coerce_program_number(snap.get(key))
        if n is not None:
            return n
        if which == "primary":
            name = str(pick.get("original_primary") or pick.get("pick_name") or "")
        else:
            name = str(pick.get("backup") or "")
        n = number_from_field_snapshot(snap.get("field") or [], name)
        if n is not None:
            return n
    return None
