"""
Harness Racing NZ (HRNZ) parser — infohorse.hrnz.co.nz fields and meeting pages.
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


BASE = "https://infohorse.hrnz.co.nz"
FIELDS_INDEX_URL = "https://infohorse.hrnz.co.nz/datahrs/fields/fields.htm"


class ParseError(RuntimeError):
    pass


# "Tue 29 Apr" or "Sun, 8 Feb" -> (day, month)
_DAY_MONTH_RE = re.compile(r"^\s*(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s*,?\s*(\d{1,2})\s+([A-Za-z]{3})\s*$", re.IGNORECASE)
# Relative meeting links on index (e.g. 021019fd.htm, 020837) so we don't miss any club (e.g. Otaki)
_MEETING_HREF_RE = re.compile(r"^[0-9A-Za-z_.-]+\.htm$|^[0-9]{4,}[A-Za-z]*$")
_TIME_12H_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*(am|pm)\s*$", re.IGNORECASE)
_RACE_NUMBER_RE = re.compile(r"Race\s+Number:\s*Race\s+(\d+)", re.IGNORECASE)
_RACE_TIME_RE = re.compile(r"Race\s+Time:\s*(\d{1,2}:\d{2}\s*[ap]m)", re.IGNORECASE)
_PARTICIPANTS_CAPTION_RE = re.compile(r"Participants\s+for\s+Race\s+(\d+)(?:\s*:\s*(.+))?", re.IGNORECASE)


def _month_num(mon: str) -> Optional[int]:
    mon = (mon or "").strip().lower()[:3]
    months = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
              "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
    return months.get(mon)


def _parse_time_12h(s: str) -> Optional[time]:
    m = _TIME_12H_RE.match((s or "").strip().replace(" ", ""))
    if not m:
        return None
    hh, mm, ap = int(m.group(1)), int(m.group(2)), m.group(3).upper()
    if hh == 12:
        hh = 0
    if ap == "PM":
        hh += 12
    if 0 <= hh <= 23 and 0 <= mm <= 59:
        return time(hour=hh, minute=mm)
    return None


def _meeting_status(now_local: datetime, meeting_date: date, first_time: Optional[time], num_races: Optional[int]) -> str:
    if first_time is None:
        return "unknown"
    start_dt = datetime.combine(meeting_date, first_time, tzinfo=now_local.tzinfo)
    if now_local < start_dt:
        return "upcoming"
    races = num_races or 10
    est_end = start_dt + timedelta(minutes=races * 30)
    if now_local <= est_end:
        return "in_progress"
    return "finished"


def fetch_meetings_for_date(meeting_date: date, *, ttl_seconds: int = 300) -> list[Meeting]:
    """
    Parse HRNZ fields index for meetings on or near the given date.
    Table has Club (link to meeting page), Date (e.g. Tue 29 Apr).
    """
    resp = get(FIELDS_INDEX_URL, ttl_seconds=ttl_seconds, timeout_seconds=25)
    soup = BeautifulSoup(resp.text, "html.parser")

    meetings: list[Meeting] = []
    now_local = datetime.now().astimezone()

    # Find table with headers Club, Date, Racebook, Last Updated
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue
        # First row may be header
        for tr in rows[1:]:
            links = tr.find_all("a", href=True)
            club_link = None
            for a in links:
                href = (a.get("href") or "").strip()
                text = (a.get_text(strip=True) or "").strip()
                # Club link: points to meeting fields page (path like .../fields/020837 or 021019fd.htm)
                if not href:
                    continue
                is_fields_url = "fields" in href.lower() or "/datahrs/" in href.lower()
                is_meeting_id = bool(_MEETING_HREF_RE.match(href.split("/")[-1].split("?")[0]))
                if not is_fields_url and not is_meeting_id:
                    continue
                # Skip Racebook/Scratchings/Custom so we get the club name link (e.g. Otaki T.C., Manawatu H.R.C.)
                if not text or len(text) < 2 or "Custom" in text or "Racebook" in text or "Scratchings" in text:
                    continue
                club_link = a
                break
            if not club_link:
                continue
            href = (club_link.get("href") or "").strip()
            venue = (club_link.get_text(strip=True) or "").strip()
            if not venue:
                continue
            # URL may be .../fields/020837 (no .htm) or .../fields/042919fd.htm
            if not href.endswith(".htm") and "/fields/" in href:
                href = href.rstrip("/")
            # Resolve meeting URL (relative to fields dir)
            meeting_url = urljoin(FIELDS_INDEX_URL, href)
            if not meeting_url.startswith("http"):
                meeting_url = urljoin(BASE + "/datahrs/fields/", href)

            # Try to get date from same row (e.g. "Tue 29 Apr")
            cells = tr.find_all(["td", "th"])
            row_date: Optional[date] = None
            for cell in cells:
                txt = (cell.get_text(strip=True) or "").strip()
                m = _DAY_MONTH_RE.match(txt)
                if m:
                    day = int(m.group(1))
                    mon = _month_num(m.group(2))
                    if mon:
                        try:
                            row_date = date(meeting_date.year, mon, day)
                        except ValueError:
                            # e.g. Feb 30
                            continue
                    break
            if row_date is None:
                row_date = meeting_date

            # Include if date matches or within 2 weeks (index may list nearby dates)
            if abs((row_date - meeting_date).days) > 14:
                continue

            meetings.append(
                Meeting(
                    code="harness",
                    source="hrnz_nz",
                    venue=venue,
                    meeting_date=row_date,
                    first_race_time_local=None,
                    num_races=None,
                    meeting_url=meeting_url,
                    status="unknown",
                    extra={"country": "NZ"},
                )
            )

    # Dedupe by meeting_url
    seen: set[str] = set()
    out: list[Meeting] = []
    for m in meetings:
        if m.meeting_url in seen:
            continue
        seen.add(m.meeting_url)
        out.append(m)
    out.sort(key=lambda m: (m.meeting_date, m.venue))
    return out


def _find_race_time_before(table) -> Optional[time]:
    """Walk backwards from table; time may be in <time> tag (e.g. 12:00pm)."""
    prev = table.find_previous()
    for _ in range(50):
        if prev is None:
            break
        txt = prev.get_text(" ", strip=True)
        m = _RACE_TIME_RE.search(txt)
        if m:
            return _parse_time_12h(m.group(1))
        # HRNZ sometimes has <time>12:00pm</time>; check for standalone time in text
        m2 = re.search(r"\b(\d{1,2}:\d{2})\s*([ap]m)\b", txt, re.IGNORECASE)
        if m2 and "Race" in txt:
            return _parse_time_12h(m2.group(1) + m2.group(2))
        prev = prev.find_previous()
    return None


def fetch_races_and_runners_for_meeting(
    meeting_url: str, meeting_date: date, *, ttl_seconds: int = 300
) -> tuple[list[Race], dict[int, list[Runner]]]:
    """
    Parse HRNZ meeting page. Each race is a table with caption "Participants for Race N: <name>"
    and header row BookBk, Form, Frm, Name, DrawDr, Driver, Trainer. Race time is in a preceding element.
    """
    resp = get(meeting_url, ttl_seconds=ttl_seconds, timeout_seconds=30)
    soup = BeautifulSoup(resp.text, "html.parser")

    races: list[Race] = []
    runners_by_race: dict[int, list[Runner]] = {}

    for table in soup.find_all("table"):
        # HRNZ uses <caption>Participants for Race 1: EAST WEST FENCING MOBILE PACE</caption>
        cap = table.find("caption")
        if not cap:
            continue
        cap_text = (cap.get_text(" ", strip=True) or "").strip()
        m_cap = _PARTICIPANTS_CAPTION_RE.match(cap_text)
        if not m_cap:
            continue
        race_no = int(m_cap.group(1))
        race_name = (m_cap.group(2) or "").strip()[:80] if m_cap.lastindex >= 2 and m_cap.group(2) else f"Race {race_no}"

        # Header row has BookBk, Form, Frm, Name, DrawDr, Driver, Trainer
        first_row = table.find("tr")
        if not first_row:
            continue
        cells = first_row.find_all(["th", "td"])
        col_names = [c.get_text(strip=True).lower() for c in cells]
        idx_name = next((i for i, c in enumerate(col_names) if c == "name"), None)
        idx_draw = next((i for i, c in enumerate(col_names) if "drawdr" in c or (c == "draw")), None)
        idx_form = next((i for i, c in enumerate(col_names) if c == "form" or c == "frm"), None)
        idx_driver = next((i for i, c in enumerate(col_names) if "driver" in c or c == "drv"), None)
        idx_trainer = next((i for i, c in enumerate(col_names) if c == "trainer"), None)
        if idx_name is None:
            continue

        race_time = _find_race_time_before(table)

        race = Race(
            code="harness",
            race_no=race_no,
            name=race_name or f"Race {race_no}",
            distance_m=None,
            start_time_local=race_time,
            race_url=meeting_url + f"#race{race_no}",
            extra={},
        )
        races.append(race)

        runners: list[Runner] = []
        for tr in table.find_all("tr")[1:]:
            tds = tr.find_all("td")
            if len(tds) <= max(idx_name, idx_draw or 0):
                continue
            name_cell = tds[idx_name] if idx_name < len(tds) else None
            name = ""
            if name_cell:
                a = name_cell.find("a")
                name = (a.get_text(strip=True) if a else name_cell.get_text(strip=True)) or ""
            if not name:
                continue
            draw = None
            if idx_draw is not None and idx_draw < len(tds):
                draw_text = (tds[idx_draw].get_text(strip=True) or "").replace("Ft", "").strip()
                try:
                    draw = int(draw_text) if draw_text.isdigit() else None
                except ValueError:
                    pass
            form_str = ""
            if idx_form is not None and idx_form < len(tds):
                form_str = (tds[idx_form].get_text(strip=True) or "")[:20]
            driver = None
            if idx_driver is not None and idx_driver < len(tds):
                driver = (tds[idx_driver].get_text(strip=True) or "").strip()[:80]
            trainer = None
            if idx_trainer is not None and idx_trainer < len(tds):
                trainer = (tds[idx_trainer].get_text(strip=True) or "").strip()[:80]

            recent: list[int] = []
            for ch in re.findall(r"[0-9xX]", form_str):
                if ch.upper() == "X":
                    continue
                try:
                    v = int(ch)
                    recent.append(10 if v == 0 else v)
                except ValueError:
                    pass

            runners.append(
                Runner(
                    code="harness",
                    name=name,
                    draw=draw,
                    recent_finishes=recent[:8],
                    early_speed=None,
                    jockey_or_driver=driver,
                    trainer=trainer,
                    raw={"cells": [name, form_str, draw], "meeting_url": meeting_url},
                )
            )

        runners_by_race[race_no] = runners

    races.sort(key=lambda r: r.race_no)
    return races, runners_by_race
