from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from typing import Optional
from urllib.parse import urljoin, urlparse, parse_qs

from bs4 import BeautifulSoup

from fetch import get
from models import Meeting, Race, Runner


BASE = "https://www.harness.org.au"
NSW_FIELDS_INDEX_URL = "https://www.harness.org.au/nsw-fields-index.cfm"


class ParseError(RuntimeError):
    pass


_DAY_HEADING_RE = re.compile(r"^(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s+(\d{1,2})\s+([A-Za-z]+)\s*$")
_RACE_CELL_RE = re.compile(r"^Race\s+(\d+)\s*$", re.IGNORECASE)
_TIME_12H_SPACE_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*(AM|PM)\s*$", re.IGNORECASE)


def _month_num(mon: str) -> Optional[int]:
    mon = (mon or "").strip().lower()
    months = {
        "january": 1,
        "february": 2,
        "march": 3,
        "april": 4,
        "may": 5,
        "june": 6,
        "july": 7,
        "august": 8,
        "september": 9,
        "october": 10,
        "november": 11,
        "december": 12,
    }
    return months.get(mon)


def _parse_time_12h_space(s: str) -> Optional[time]:
    m = _TIME_12H_SPACE_RE.match((s or "").strip())
    if not m:
        return None
    hh = int(m.group(1))
    mm = int(m.group(2))
    ap = m.group(3).upper()
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


def _form_url_from_meeting_link(meeting_url: str) -> str:
    """
    Convert a meeting link like:
      /fields.cfm?mc=EY060226&fromstate=nsw
    into:
      /form.cfm?mc=EY060226&fromstate=nsw
    """
    parsed = urlparse(meeting_url)
    qs = parse_qs(parsed.query)
    mc = (qs.get("mc") or [""])[0]
    fromstate = (qs.get("fromstate") or [""])[0]
    if not mc:
        raise ParseError("Missing mc= parameter on harness meeting link.")
    extra = f"&fromstate={fromstate}" if fromstate else ""
    return f"{BASE}/form.cfm?mc={mc}{extra}"


def fetch_meetings_for_date(meeting_date: date, *, ttl_seconds: int = 300) -> list[Meeting]:
    """
    Parse NSW harness meetings for a given date from the NSW fields index.
    """
    resp = get(NSW_FIELDS_INDEX_URL, ttl_seconds=ttl_seconds)
    soup = BeautifulSoup(resp.text, "html.parser")

    meetings: list[Meeting] = []

    current_heading_date: Optional[date] = None

    # Walk table rows; meeting rows have 3 linked cells:
    #   [Venue link] [NSW link] [Day/Night link]
    for tr in soup.find_all("tr"):
        txt = (tr.get_text(" ", strip=True) or "").strip()
        m = _DAY_HEADING_RE.match(txt)
        if m:
            day = int(m.group(2))
            mon = _month_num(m.group(3))
            if mon:
                try:
                    current_heading_date = date(meeting_date.year, mon, day)
                except Exception:
                    current_heading_date = None
            continue

        if current_heading_date != meeting_date:
            continue

        links = tr.find_all("a")
        if not links:
            continue
        # choose first fields.cfm link with a non-trivial venue label
        venue_link = None
        for a in links:
            href = a.get("href") or ""
            label = (a.get_text(" ", strip=True) or "").strip()
            if "fields.cfm?mc=" not in href:
                continue
            if not label:
                continue
            if label.upper() in {"NSW", "DAY", "NIGHT", "TWILIGHT"}:
                continue
            venue_link = a
            break
        if venue_link is None:
            continue

        href = venue_link.get("href") or ""
        venue = (venue_link.get_text(" ", strip=True) or "").strip()
        meeting_link = urljoin(BASE, href)
        meeting_url = _form_url_from_meeting_link(meeting_link)

        meetings.append(
            Meeting(
                code="harness",
                source="harness_org_au",
                venue=venue,
                meeting_date=meeting_date,
                first_race_time_local=None,
                num_races=None,
                meeting_url=meeting_url,
                status="unknown",
                extra={"meeting_link": meeting_link},
            )
        )

    uniq = {m.meeting_url: m for m in meetings}
    out = list(uniq.values())
    out.sort(key=lambda m: m.venue)
    return out


def fetch_races_and_runners_for_meeting(meeting_url: str, meeting_date: date, *, ttl_seconds: int = 300) -> tuple[list[Race], dict[int, list[Runner]]]:
    """
    Parse a harness.org.au form guide page into races and runners.

    This page is very verbose; we rely on a stable pattern:
    - A 1-row table like: [Race 1, <race name>, <time>]
    - Followed by a large multi-row table with runner summary rows containing:
        [<big summary>, '1 HORSE', 'Fr1', ...]
    """
    resp = get(meeting_url, ttl_seconds=ttl_seconds, timeout_seconds=30)
    soup = BeautifulSoup(resp.text, "html.parser")

    races: list[Race] = []
    runners_by_race: dict[int, list[Runner]] = {}

    # Find all "Race N" header tables (1 row with first cell "Race N")
    header_tables: list[tuple[int, list[str], Any]] = []
    for tbl in soup.find_all("table"):
        trs = tbl.find_all("tr")
        if len(trs) != 1:
            continue
        cells = [c.get_text(" ", strip=True) for c in trs[0].find_all(["td", "th"])]
        if len(cells) < 2:
            continue
        m = _RACE_CELL_RE.match((cells[0] or "").strip())
        if not m:
            continue
        header_tables.append((int(m.group(1)), cells, tbl))

    header_tables.sort(key=lambda x: x[0])

    for idx, (race_no, cells, tbl) in enumerate(header_tables):
        race_name = (cells[1] or "").strip() if len(cells) >= 2 else f"Race {race_no}"
        start_time = None
        if len(cells) >= 3:
            start_time = _parse_time_12h_space(cells[2])

        races.append(
            Race(
                code="harness",
                race_no=race_no,
                name=race_name or f"Race {race_no}",
                distance_m=None,
                start_time_local=start_time,
                race_url=meeting_url + f"#race{race_no}",
                extra={},
            )
        )

        # runner blocks are typically multiple small tables after the header, one per runner,
        # until the next race header table.
        end_tbl = header_tables[idx + 1][2] if idx + 1 < len(header_tables) else None

        rs: list[Runner] = []
        cursor = tbl
        for _ in range(500):
            cursor = cursor.find_next("table")
            if cursor is None or cursor is end_tbl:
                break
            # Skip other race header tables if any nested weirdly
            if cursor == end_tbl:
                break

            trs = cursor.find_all("tr")
            if not trs:
                continue
            tds = trs[0].find_all("td")
            if len(tds) < 3:
                continue
            row_cells = [td.get_text(" ", strip=True) for td in tds]
            name_cell = (row_cells[1] or "").strip()
            draw_cell = (row_cells[2] or "").strip()
            if not re.match(r"^\d+\s+\S+", name_cell):
                continue
            if not re.search(r"\b(?:Fr|Sr)\s*\d{1,2}\b", draw_cell, re.IGNORECASE):
                continue

            name = re.sub(r"^\d+\s+", "", name_cell).strip()
            draw = None
            m_draw = re.search(r"\b(?:Fr|Sr)\s*(\d{1,2})\b", draw_cell, re.IGNORECASE)
            if m_draw:
                try:
                    draw = int(m_draw.group(1))
                except Exception:
                    draw = None

            summary = row_cells[0]
            driver = None
            trainer = None
            m_driver = re.search(r"Driver:\s*([^|]+?)(?:Owner:|Trainer:|Career:|$)", summary, re.IGNORECASE)
            if m_driver:
                driver = m_driver.group(1).strip()
            m_tr = re.search(r"Trainer:\s*([^|]+?)(?:Career:|Last Win|$)", summary, re.IGNORECASE)
            if m_tr:
                trainer = m_tr.group(1).strip()

            # extra historical-ish stats commonly present in the summary text
            career = None
            bmr = None
            lts = None
            m_career = re.search(r"\bCareer:\s*([0-9]+-\d+-\d+-\d+)\b", summary, re.IGNORECASE)
            if m_career:
                career = m_career.group(1)
            m_bmr = re.search(r"\bBMR:\s*([0-9:\.A-Z]+)\b", summary, re.IGNORECASE)
            if m_bmr:
                bmr = m_bmr.group(1)
            m_lts = re.search(r"\bLTS:\s*[$]?([0-9,]+)\b", summary, re.IGNORECASE)
            if m_lts:
                lts = m_lts.group(1)

            recent: list[int] = []
            m_seq = re.search(r"\bs([0-9xX]{4,12})\b", summary)
            if m_seq:
                seq = m_seq.group(1)
                for ch in re.findall(r"[0-9xX]", seq):
                    if ch.lower() == "x":
                        continue
                    v = int(ch)
                    recent.append(10 if v == 0 else v)

            rs.append(
                Runner(
                    code="harness",
                    name=name,
                    draw=draw,
                    recent_finishes=recent[:5],
                    early_speed=None,
                    trainer=trainer,
                    jockey_or_driver=driver,
                    last10=m_seq.group(1) if m_seq else None,
                    raw={
                        "cells": row_cells,
                        "meeting_url": meeting_url,
                        "summary": summary,
                        "career": career,
                        "bmr": bmr,
                        "lts": lts,
                    },
                )
            )

        runners_by_race[race_no] = rs

    races.sort(key=lambda r: r.race_no)
    return races, runners_by_race


def enrich_meeting_with_first_race(meeting: Meeting, races: list[Race]) -> Meeting:
    first_time = races[0].start_time_local if races else None
    now_local = datetime.now().astimezone()
    status = _meeting_status(now_local, meeting.meeting_date, first_time, num_races=len(races) if races else None)
    return Meeting(
        code=meeting.code,
        source=meeting.source,
        venue=meeting.venue,
        meeting_date=meeting.meeting_date,
        first_race_time_local=first_time,
        num_races=len(races) if races else meeting.num_races,
        meeting_url=meeting.meeting_url,
        status=status,
        extra=meeting.extra,
    )

