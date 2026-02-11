"""
NZ Thoroughbred (nzracing.co.nz / LOVERACING.NZ) parser.
Returns Meeting, Race, Runner compatible with models.py.
"""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from typing import Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from fetch import get
from models import Meeting, Race, Runner


BASE = "https://www.nzracing.co.nz"
NOM_FIELDS_URL = "https://www.nzracing.co.nz/raceinfo/nom-fields.aspx"

# Link to meeting overview: /raceinfo/54896/meeting-overview.aspx
_MEETING_LINK_RE = re.compile(r"/raceinfo/(\d+)/meeting-overview\.aspx", re.IGNORECASE)
# Date in page: "7 Feb", "8 Feb"
_DAY_MONTH_RE = re.compile(r"^\s*(\d{1,2})\s+([A-Za-z]{3})\s*$", re.IGNORECASE)


class ParseError(RuntimeError):
    pass


def _month_num(mon: str) -> Optional[int]:
    mon = (mon or "").strip().lower()[:3]
    months = {
        "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
        "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    }
    return months.get(mon)


def _meeting_status(
    now_local: datetime, meeting_date: date,
    first_time: Optional[time], num_races: Optional[int],
) -> str:
    if first_time is None:
        return "unknown"
    start_dt = datetime.combine(meeting_date, first_time, tzinfo=now_local.tzinfo)
    if now_local < start_dt:
        return "upcoming"
    races = num_races or 8
    est_end = start_dt + timedelta(minutes=races * 35)
    if now_local <= est_end:
        return "in_progress"
    return "finished"


def fetch_meetings_for_date(meeting_date: date, *, ttl_seconds: int = 300) -> list[Meeting]:
    """
    Parse nzracing.co.nz nom-fields page for meetings on or near the given date.
    Page lists dates (e.g. "7 Feb", "8 Feb") with "Today"/"Yesterday"/"Tomorrow" and
    links to raceinfo/{id}/meeting-overview.aspx. We associate each link with the
    preceding date block and filter by meeting_date (±7 days).
    """
    resp = get(NOM_FIELDS_URL, ttl_seconds=ttl_seconds, timeout_seconds=25)
    soup = BeautifulSoup(resp.text, "html.parser")
    now_local = datetime.now().astimezone()
    year = meeting_date.year

    # Walk document in order; associate meeting links with the most recent date label.
    current_block_date: Optional[date] = None
    meetings_out: list[Meeting] = []
    seen_urls: set[str] = set()
    from datetime import timedelta as _td

    for el in soup.descendants:
        if getattr(el, "name", None) == "a" and el.get("href"):
            href = (el.get("href") or "").strip()
            m = _MEETING_LINK_RE.search(href)
            if m:
                meeting_id = m.group(1)
                venue = (el.get_text(" ", strip=True) or "").strip()
                if not venue or "Noms" in venue or "Fields" in venue or "Results" in venue or "Calendar" in venue:
                    continue
                full_url = urljoin(BASE, href)
                if full_url in seen_urls:
                    continue
                seen_urls.add(full_url)
                row_date = current_block_date if current_block_date is not None else meeting_date
                if row_date != meeting_date:
                    continue
                meetings_out.append(
                    Meeting(
                        code="thoroughbred",
                        source="nzracing",
                        venue=venue,
                        meeting_date=row_date,
                        first_race_time_local=None,
                        num_races=None,
                        meeting_url=full_url,
                        status=_meeting_status(now_local, row_date, None, None),
                        extra={"country": "NZ", "meeting_id": meeting_id},
                    )
                )
            continue
        # Update current block date from text like "7 Feb" or "Today"
        text = ""
        if hasattr(el, "strip"):
            text = (str(el) or "").strip()
        elif hasattr(el, "get_text"):
            text = (el.get_text(" ", strip=True) or "").strip()
        if not text or len(text) > 50:
            continue
        lower = text.lower()
        if "today" in lower:
            current_block_date = meeting_date
        elif "yesterday" in lower:
            current_block_date = meeting_date - _td(days=1)
        elif "tomorrow" in lower:
            current_block_date = meeting_date + _td(days=1)
        else:
            # Match "7 Feb" or "8 Feb" anywhere in the text
            for part in text.replace("\n", " ").split():
                dm = _DAY_MONTH_RE.match(part.strip())
                if dm:
                    try:
                        day = int(dm.group(1))
                        mon = _month_num(dm.group(2))
                        if mon:
                            current_block_date = date(year, mon, day)
                            break
                    except (ValueError, TypeError):
                        pass

    # Fallback: if no meetings (e.g. date labels not found), take all meeting links for meeting_date
    if not meetings_out:
        for a in soup.find_all("a", href=True):
            href = (a.get("href") or "").strip()
            m = _MEETING_LINK_RE.search(href)
            if not m:
                continue
            venue = (a.get_text(" ", strip=True) or "").strip()
            if not venue or "Noms" in venue or "Fields" in venue or "Results" in venue or "Calendar" in venue:
                continue
            full_url = urljoin(BASE, href)
            meetings_out.append(
                Meeting(
                    code="thoroughbred",
                    source="nzracing",
                    venue=venue,
                    meeting_date=meeting_date,
                    first_race_time_local=None,
                    num_races=None,
                    meeting_url=full_url,
                    status=_meeting_status(now_local, meeting_date, None, None),
                    extra={"country": "NZ", "meeting_id": m.group(1)},
                )
            )

    # Dedupe by URL and sort
    by_url: dict[str, Meeting] = {}
    for m in meetings_out:
        if m.meeting_url not in by_url:
            by_url[m.meeting_url] = m
    return sorted(by_url.values(), key=lambda x: (x.meeting_date, x.venue))


def fetch_races_and_runners_for_meeting(
    meeting_url: str, meeting_date: date, *, ttl_seconds: int = 300
) -> tuple[list[Race], dict[int, list[Runner]], dict]:
    """
    NZ TB: race/runner parsing from nzracing.co.nz not yet implemented.
    Returns empty races and runners so the app can show the meeting in the roster/grid.
    """
    _ = meeting_url
    _ = meeting_date
    _ = ttl_seconds
    return ([], {}, {})
