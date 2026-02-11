"""
Sky Racing schedule overlay — schedule.skyracing.com.au.
Fetches Sky Racing 1 and Sky Racing 2 schedule for a given date.
Returns list of dicts for overlay/comparison with our grid (no auth, public HTML).
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Optional
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from fetch import get


BASE = "https://schedule.skyracing.com.au"
# Sky Racing 1 = channel Sky, Sky Racing 2 = channel SR2
SCHEDULE_URL = f"{BASE}/schedule.php"

# ForwardToMeeting(228414411, 1) -> meeting_id, race_no
_FORWARD_RE = re.compile(r"ForwardToMeeting\s*\(\s*(\d+)\s*,\s*(\d+)\s*\)", re.IGNORECASE)
# Date in first cell: Sunday01/02/26 or 01/02/26 (DD/MM/YY or DD/MM/YYYY)
_DATE_RE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{2,4})\s*$", re.IGNORECASE)


def _normalize_venue(raw: str) -> str:
    """Strip (T), (D), (M), (N) suffix for matching."""
    s = (raw or "").strip()
    return re.sub(r"\s*\([TDMN]\)\s*$", "", s, flags=re.IGNORECASE).strip() or s


def _parse_row_date(cell_text: str, ref_date: date) -> Optional[date]:
    """Parse 'Sunday01/02/26' or '01/02/26' -> date. Use ref_date for year if 2-digit."""
    m = _DATE_RE.search((cell_text or "").strip())
    if not m:
        return None
    day = int(m.group(1))
    month = int(m.group(2))
    y = int(m.group(3))
    if y < 100:
        y += 2000 if y < 80 else 1900
    try:
        return date(y, month, day)
    except ValueError:
        return None


def _parse_schedule_html(html: str, channel_label: str, ref_date: date) -> list[dict]:
    """Parse one schedule.php HTML page. channel_label 'Sky' -> SKY1, 'SR2' -> SKY2."""
    soup = BeautifulSoup(html, "html.parser")
    channel = "SKY1" if channel_label.lower() == "sky" else "SKY2"
    out: list[dict] = []

    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        current_row_date: Optional[date] = None
        for tr in rows:
            cells = tr.find_all(["td", "th"])
            if not cells:
                continue
            first_text = (cells[0].get_text(" ", strip=True) or "").strip()
            row_date = _parse_row_date(first_text, ref_date)
            if row_date is not None:
                current_row_date = row_date
            if current_row_date is None:
                continue
            for cell in cells:
                for a in cell.find_all("a"):
                    onclick = a.get("onclick") or ""
                    js = onclick.strip()
                    m = _FORWARD_RE.search(js)
                    if not m:
                        continue
                    meeting_id = m.group(1)
                    try:
                        race_no = int(m.group(2))
                    except ValueError:
                        continue
                    raw_venue = (a.get_text(" ", strip=True) or "").strip()
                    venue = _normalize_venue(raw_venue)
                    if not venue:
                        continue
                    # Schedule page does not provide race times; use None (user can compare by venue+race only)
                    dt_local: Optional[datetime] = None
                    dt_app_tz: Optional[datetime] = None
                    raw = f"{venue} R{race_no}"
                    out.append({
                        "channel": channel,
                        "venue": venue,
                        "track_code": meeting_id,
                        "race_no": race_no,
                        "date": current_row_date,
                        "dt_local": dt_local,
                        "dt_app_tz": dt_app_tz,
                        "raw": raw,
                    })
    return out


def fetch_sky_schedule(
    schedule_date: date,
    *,
    ttl_seconds: Optional[int] = 300,
    timeout_seconds: float = 25.0,
) -> list[dict]:
    """
    Fetch Sky Racing 1 and Sky Racing 2 schedule for the given date.
    Returns list of dicts with: channel ("SKY1" | "SKY2"), venue, track_code (meeting id),
    race_no (int), dt_local (None if not provided), dt_app_tz (None), raw (debug text).
    Uses schedule.skyracing.com.au schedule.php (public HTML). Best-effort; no auth.
    """
    month = schedule_date.month
    year = schedule_date.year
    all_entries: list[dict] = []

    for channel_param, channel_label in [("Sky", "Sky"), ("SR2", "SR2")]:
        url = f"{SCHEDULE_URL}?m={month}&y={year}&channel={channel_param}"
        try:
            resp = get(url, ttl_seconds=ttl_seconds or 300, timeout_seconds=timeout_seconds)
            entries = _parse_schedule_html(resp.text, channel_label, schedule_date)
            # Only include entries for the requested date
            for e in entries:
                if e.get("date") == schedule_date:
                    all_entries.append(e)
        except Exception:
            # Best-effort: skip this channel on error
            pass

    return all_entries
