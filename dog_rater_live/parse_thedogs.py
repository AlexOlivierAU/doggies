from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from typing import Any, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from fetch import FetchError, get
from models import Meeting, Race, Runner


BASE = "https://www.thedogs.com.au"
RACECARDS_URL = "https://www.thedogs.com.au/racing/racecards"


class ParseError(RuntimeError):
    pass


_DATE_HEADING_RE = re.compile(r"^\s*(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})\s*$")
_RACES_RE = re.compile(r"(\d+)\s*races", re.IGNORECASE)
_TIME_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*$")


def _parse_date_heading(s: str) -> Optional[date]:
    m = _DATE_HEADING_RE.match(s.strip())
    if not m:
        return None
    day = int(m.group(1))
    mon = m.group(2).strip().lower()
    year = int(m.group(3))
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
    if mon not in months:
        return None
    return date(year, months[mon], day)


def _parse_time(s: str) -> Optional[time]:
    m = _TIME_RE.match(s.strip())
    if not m:
        return None
    hh = int(m.group(1))
    mm = int(m.group(2))
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return None
    return time(hour=hh, minute=mm)


def _meeting_status(
    *,
    now_local: datetime,
    meeting_date: date,
    first_time: Optional[time],
    num_races: Optional[int],
) -> str:
    if first_time is None:
        return "unknown"
    start_dt = datetime.combine(meeting_date, first_time, tzinfo=now_local.tzinfo)
    if now_local < start_dt:
        return "upcoming"
    # crude duration estimate: greyhound races often ~20-25 min apart including gaps
    races = num_races or 12
    est_end = start_dt + timedelta(minutes=races * 25)
    if now_local <= est_end:
        return "in_progress"
    return "finished"


def fetch_meetings_for_date(meeting_date: date, *, ttl_seconds: int = 120) -> list[Meeting]:
    """
    Parse meetings for a given date from the public Fields page.
    Robustness strategy: parse by date headings and nearby meeting cards.
    """
    resp = get(RACECARDS_URL, ttl_seconds=ttl_seconds)
    soup = BeautifulSoup(resp.text, "html.parser")

    # Robustness approach:
    # - Find the specific date heading element (e.g. "06 February 2026")
    # - Walk forward through the DOM until the next date heading
    # - Extract meeting links in that segment
    meetings: list[Meeting] = []
    now_local = datetime.now().astimezone()

    target_heading = meeting_date.strftime("%d %B %Y")
    heading_el = None
    for el in soup.find_all(True):
        if (el.get_text(" ", strip=True) or "").strip() == target_heading:
            heading_el = el
            break
    if heading_el is None:
        return []

    for node in heading_el.next_elements:
        if getattr(node, "name", None):
            txt = (node.get_text(" ", strip=True) or "").strip()
            d = _parse_date_heading(txt)
            if d is not None and d != meeting_date:
                break

        if getattr(node, "name", None) != "a":
            continue

        href = node.get("href") or ""
        if not href.startswith("/racing/"):
            continue
        # Meeting URL shape: /racing/<track-slug>/<yyyy-mm-dd>?trial=false
        # Race URL shape: /racing/<track-slug>/<yyyy-mm-dd>/<raceNo>/...
        if re.search(r"/\d{4}-\d{2}-\d{2}/\d+/", href):
            continue
        if not re.search(r"/\d{4}-\d{2}-\d{2}", href):
            continue

        # Meeting card anchor text includes venue and state/races, but the exact formatting can vary.
        text = node.get_text(" ", strip=True)
        parts = [p for p in re.split(r"\s+", text) if p]
        if not parts:
            continue

        venue = parts[0]
        # Sometimes venue can be multi-word (e.g. "Wentworth Park", "Ladbrokes Gardens").
        # Heuristic: venue ends before token containing "races" or a state code block.
        venue_tokens: list[str] = []
        for token in parts:
            if _RACES_RE.search(token):
                break
            if _TIME_RE.match(token):
                break
            # Skip obvious state abbreviations if they appear as standalone tokens (NSW/VIC/QLD/SA/WA/TAS/NT/ACT)
            if token.upper() in {"NSW", "VIC", "QLD", "SA", "WA", "TAS", "NT", "ACT"}:
                break
            venue_tokens.append(token)
        if venue_tokens:
            venue = " ".join(venue_tokens)

        num_races = None
        m_races = _RACES_RE.search(text)
        if m_races:
            try:
                num_races = int(m_races.group(1))
            except Exception:
                num_races = None

        # find first time token
        first_time = None
        for token in parts:
            t = _parse_time(token)
            if t is not None:
                first_time = t
                break

        meeting_url = urljoin(BASE, href)
        status = _meeting_status(
            now_local=now_local, meeting_date=meeting_date, first_time=first_time, num_races=num_races
        )

        meetings.append(
            Meeting(
                code="greyhound",
                source="thedogs",
                venue=venue,
                meeting_date=meeting_date,
                first_race_time_local=first_time,
                num_races=num_races,
                meeting_url=meeting_url,
                status=status,
            )
        )

    # Deduplicate by URL (same link may appear multiple times in nav sections)
    seen: set[str] = set()
    out: list[Meeting] = []
    for m in meetings:
        if m.meeting_url in seen:
            continue
        seen.add(m.meeting_url)
        out.append(m)
    out.sort(key=lambda m: (m.first_race_time_local or time(23, 59), m.venue))
    return out


def fetch_races_for_meeting(meeting_url: str, *, ttl_seconds: int = 120) -> list[Race]:
    """
    Parse race list from a meeting page.
    Robustness strategy: scan for links that look like /racing/<track>/<date>/<raceNo>/...
    """
    resp = get(meeting_url, ttl_seconds=ttl_seconds)
    soup = BeautifulSoup(resp.text, "html.parser")

    races_by_no: dict[int, Race] = {}

    for a in soup.find_all("a"):
        href = a.get("href") or ""
        if not href.startswith("/racing/"):
            continue
        m = re.search(r"/racing/[^/]+/\d{4}-\d{2}-\d{2}/(\d+)(?:/|$)", href)
        if not m:
            continue
        race_no = int(m.group(1))
        race_url = urljoin(BASE, href)

        label = a.get_text(" ", strip=True) or f"Race {race_no}"
        label = re.sub(r"\s+", " ", label).strip()

        # distance often appears like "300m" near the link text or in surrounding text
        distance_m = None
        near = " ".join(
            x.get_text(" ", strip=True)
            for x in [a.parent, a.parent.parent if a.parent else None]
            if x is not None
        )
        m_dist = re.search(r"(\d{3,4})\s*m\b", near, re.IGNORECASE)
        if m_dist:
            try:
                distance_m = int(m_dist.group(1))
            except Exception:
                distance_m = None

        # start time sometimes appears as HH:MM in the same card
        start_time = None
        m_time = re.search(r"\b(\d{1,2}:\d{2})\b", near)
        if m_time:
            start_time = _parse_time(m_time.group(1))

        existing = races_by_no.get(race_no)
        if existing is None:
            races_by_no[race_no] = Race(
                code="greyhound",
                race_no=race_no,
                name=label,
                distance_m=distance_m,
                start_time_local=start_time,
                race_url=race_url,
            )

    races = list(races_by_no.values())
    races.sort(key=lambda r: r.race_no)
    if not races:
        raise ParseError("Could not find any race links on meeting page (layout may have changed).")
    return races


def fetch_runners_for_race(race_url: str, *, ttl_seconds: int = 120) -> list[Runner]:
    """
    Parse runner table from a race page.

    We keep this intentionally heuristic and defensive:
    - find the largest table with 'Box'/'No' and 'Runner'/'Dog' like headers
    - extract dog name, box number
    - attempt to parse "last 5" finishes from any column containing patterns like 1-2-3-...
    - attempt to parse early speed / split time from a column containing 'split'/'first' and a float
    """
    resp = get(race_url, ttl_seconds=ttl_seconds)
    soup = BeautifulSoup(resp.text, "html.parser")

    tables = soup.find_all("table")
    if not tables:
        raise ParseError("Could not find any tables on race page (layout may have changed).")

    def score_table(tbl) -> int:
        thead = tbl.find("thead")
        if not thead:
            return 0
        headers = [th.get_text(" ", strip=True).lower() for th in thead.find_all(["th", "td"])]
        score = 0
        if any("box" in h or h == "no" or "trap" in h for h in headers):
            score += 2
        if any("dog" in h or "runner" in h or "greyhound" in h or "name" in h for h in headers):
            score += 2
        if any("last" in h or "form" in h for h in headers):
            score += 1
        if any("split" in h or "first" in h or "early" in h for h in headers):
            score += 1
        # bigger table tends to be the runner table
        score += len(tbl.find_all("tr")) // 5
        return score

    tables_sorted = sorted(tables, key=score_table, reverse=True)
    runner_table = tables_sorted[0]

    # Build header -> index
    headers: list[str] = []
    thead = runner_table.find("thead")
    if thead:
        headers = [th.get_text(" ", strip=True) for th in thead.find_all(["th", "td"])]
    headers_norm = [h.strip().lower() for h in headers]

    def col_idx(*needles: str) -> Optional[int]:
        for i, h in enumerate(headers_norm):
            for n in needles:
                if n in h:
                    return i
        return None

    idx_box = col_idx("box", "trap", "no")
    idx_name = col_idx("dog", "runner", "greyhound", "name")
    idx_form = col_idx("last", "form")
    idx_split = col_idx("split", "first", "early", "1st", "1st sec", "av 1")

    def clean_name(s: str) -> str:
        s = re.sub(r"\s+", " ", (s or "")).strip()
        # Common patterns on thedogs race tables:
        # "Dog Name NBT T: Trainer R/T: GR" -> "Dog Name"
        for sep in [" NBT", " T:", "R/T:", "R/T", "Trainer:"]:
            if sep in s:
                s = s.split(sep, 1)[0].strip()
        # Drop trailing time-ish token sometimes embedded in NAME (e.g. "Money Goes 20.24")
        s = re.sub(r"\s+\d{1,2}\.\d{1,3}$", "", s).strip()
        return s

    runners: list[Runner] = []
    tbodies = runner_table.find_all("tbody")
    row_nodes = []
    if tbodies:
        for tb in tbodies:
            row_nodes.extend(tb.find_all("tr"))
    else:
        row_nodes = runner_table.find_all("tr")

    for tr in row_nodes:
        tds = tr.find_all(["td", "th"])
        if not tds:
            continue
        cells = [td.get_text(" ", strip=True) for td in tds]

        def safe_get(i: Optional[int]) -> str:
            if i is None:
                return ""
            if 0 <= i < len(cells):
                return cells[i]
            return ""

        raw_box = safe_get(idx_box)
        raw_name = safe_get(idx_name)
        raw_form = safe_get(idx_form)
        raw_split = safe_get(idx_split)

        cols = {}
        if headers and len(headers) == len(cells):
            for h, v in zip(headers, cells):
                if h and v:
                    cols[h.strip()] = v.strip()

        # If header detection failed, fall back to heuristics:
        if not raw_name:
            raw_name = max(cells, key=lambda s: len(s), default="")
        dog_name = clean_name(raw_name)
        if not dog_name or dog_name.lower() in {"scratched", "vacant"}:
            continue

        box = None
        m_box = re.search(r"\b(\d{1,2})\b", raw_box)
        if m_box:
            try:
                box = int(m_box.group(1))
            except Exception:
                box = None
        if box is None and tds:
            # thedogs often uses an SVG sprite like <sprite-svg name="rug_1"> for box numbers
            svg = tds[0].find("sprite-svg")
            if svg and svg.has_attr("name"):
                m_rug = re.search(r"rug[_-](\d{1,2})", str(svg.get("name")), re.IGNORECASE)
                if m_rug:
                    try:
                        box = int(m_rug.group(1))
                    except Exception:
                        box = None
        if box is None and cells:
            # Fallback: sometimes the first column is the box number (1-8).
            m0 = re.fullmatch(r"\s*(\d{1,2})\s*", cells[0] or "")
            if m0:
                try:
                    v = int(m0.group(1))
                    if 1 <= v <= 12:
                        box = v
                except Exception:
                    pass

        recent_finishes: list[int] = []
        # parse sequences like "1-2-3-4-5" or "1 2 3 4 5"
        form_text = raw_form or " ".join(cells)
        m_seq = re.search(r"(\d{1,2}(?:\s*[-/,\s]\s*\d{1,2}){2,})", form_text)
        if m_seq:
            nums = re.findall(r"\d{1,2}", m_seq.group(1))
            for n in nums[:5]:
                try:
                    v = int(n)
                    if 1 <= v <= 12:
                        recent_finishes.append(v)
                except Exception:
                    pass
        else:
            # Handle compact strings like "4243" (common for LAST 4).
            compact = re.sub(r"\s+", "", raw_form or "")
            if compact.isdigit() and 3 <= len(compact) <= 6:
                for ch in compact[:5]:
                    try:
                        v = int(ch)
                        if 1 <= v <= 12:
                            recent_finishes.append(v)
                    except Exception:
                        pass

        early_speed = None
        # Split time often floats like 5.38; keep conservative to avoid grabbing full race time (e.g. 20.xx).
        def parse_split_float(txt: str) -> Optional[float]:
            m = re.search(r"\b(\d{1,2}\.\d{1,3})\b", txt or "")
            if not m:
                return None
            try:
                v = float(m.group(1))
            except Exception:
                return None
            if 3.0 <= v <= 10.0:
                return v
            return None

        early_speed = parse_split_float(raw_split)
        if early_speed is None and idx_split is None:
            early_speed = parse_split_float(" ".join(cells))

        runners.append(
            Runner(
                code="greyhound",
                name=dog_name,
                draw=box,
                recent_finishes=recent_finishes,
                early_speed=early_speed,
                raw={
                    "cells": cells,
                    "headers": headers,
                    "cols": cols,
                    "detected": {
                        "idx_box": idx_box,
                        "idx_name": idx_name,
                        "idx_form": idx_form,
                        "idx_split": idx_split,
                    },
                },
            )
        )

    if not runners:
        raise ParseError("Could not parse any runners from the detected runner table.")
    return runners


def next_upcoming_meeting(today: date) -> Optional[Meeting]:
    """
    Return the next upcoming meeting for today; if none left, return first tomorrow.
    """
    now = datetime.now().astimezone()
    todays = fetch_meetings_for_date(today)

    def meeting_dt(m: Meeting) -> Optional[datetime]:
        if m.first_race_time_local is None:
            return None
        return datetime.combine(m.meeting_date, m.first_race_time_local, tzinfo=now.tzinfo)

    future = []
    for m in todays:
        dt = meeting_dt(m)
        if dt is not None and dt >= now:
            future.append((dt, m))
    if future:
        future.sort(key=lambda x: x[0])
        return future[0][1]

    tomorrow = today + timedelta(days=1)
    tom = fetch_meetings_for_date(tomorrow)
    if not tom:
        return None
    return tom[0]


def countdown_to_meeting(m: Meeting) -> Optional[str]:
    if m.first_race_time_local is None:
        return None
    now = datetime.now().astimezone()
    start = datetime.combine(m.meeting_date, m.first_race_time_local, tzinfo=now.tzinfo)
    delta = start - now
    if delta.total_seconds() < 0:
        return "already started"
    mins = int(delta.total_seconds() // 60)
    hours = mins // 60
    minutes = mins % 60
    if hours > 0:
        return f"starts in {hours}h {minutes}m"
    return f"starts in {minutes}m"


def try_fetch_meetings_fallback(_meeting_date: date) -> list[Meeting]:
    """
    Placeholder fallback parser (best-effort).
    Kept minimal in v0; returns [] with friendly errors handled in caller.
    """
    # Many times this site is protected by bot mitigation and may return 403.
    # We keep this as best-effort only.
    ras_url = "https://www.racingandsports.com.au/form-guide/greyhound"
    try:
        resp = get(
            ras_url,
            ttl_seconds=300,
            headers={
                "User-Agent": DEFAULT_FALLBACK_UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-AU,en;q=0.9",
            },
        )
    except FetchError as e:
        raise ParseError(f"Fallback source unavailable: {e}") from e

    html = resp.text or ""
    if "Just a moment" in html and "cloudflare" in html.lower():
        raise ParseError("Fallback source is protected (Cloudflare challenge).")

    soup = BeautifulSoup(html, "html.parser")
    meetings: list[Meeting] = []
    # Heuristic: collect prominent links that look like per-meeting form guides.
    for a in soup.find_all("a"):
        href = a.get("href") or ""
        txt = a.get_text(" ", strip=True) or ""
        if not txt or len(txt) < 3:
            continue
        if "greyhound" not in href and "greyhound" not in txt.lower():
            continue
        if "form-guide" not in href and "form guide" not in txt.lower():
            continue
        # Best-effort: we often cannot reliably infer date/times from this page without deeper parsing.
        meetings.append(
            Meeting(
                source="racingandsports",
                venue=txt,
                meeting_date=_meeting_date,
                first_race_time_local=None,
                num_races=None,
                meeting_url=href if href.startswith("http") else urljoin("https://www.racingandsports.com.au", href),
                status="unknown",
            )
        )
        if len(meetings) >= 20:
            break

    return meetings


# A separate UA constant for the fallback site (some sites are picky).
DEFAULT_FALLBACK_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/121.0.0.0 Safari/537.36"
)

