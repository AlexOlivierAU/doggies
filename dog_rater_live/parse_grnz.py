"""
GRNZ (Greyhound Racing New Zealand) parser — www.grnz.co.nz.
Fetches meetings for a date so NZ tracks (e.g. Hatrick Straight) appear in the roster.
Returns Meeting list; races/runners are not yet implemented (would need per-meeting fields page).
"""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from typing import Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from fetch import get
from models import Meeting, Race, Runner


BASE = "https://www.grnz.co.nz"
FIELDS_URL = "https://www.grnz.co.nz/catch-the-action/fields.aspx"
FIELDS_ERTS_URL = "https://www.grnz.co.nz/catch-the-action/Fields-and-ERTs.aspx"
CALENDAR_URL = "https://www.grnz.co.nz/catch-the-action/calendar.aspx"


class ParseError(RuntimeError):
    pass


_DATE_SLUG_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_TIME_RE = re.compile(r"\b(\d{1,2}):(\d{2})\b")


def _meeting_status(
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
    races = num_races or 10
    est_end = start_dt + timedelta(minutes=races * 25)
    if now_local <= est_end:
        return "in_progress"
    return "finished"


def _date_formats_for_match(meeting_date: date) -> list[str]:
    """Formats to look for in GRNZ page (they may use dd/mm/yyyy or dd-mm-yyyy)."""
    fmts = [
        meeting_date.strftime("%Y-%m-%d"),
        meeting_date.strftime("%d-%m-%Y"),
        meeting_date.strftime("%d/%m/%Y"),
        meeting_date.strftime("%d %b %Y"),  # 06 Feb 2025
    ]
    # d/m/yyyy without leading zeros (portable)
    fmts.append(f"{meeting_date.day}/{meeting_date.month}/{meeting_date.year}")
    return fmts


def fallback_hatrick_straight_meeting(meeting_date: date) -> Meeting:
    """
    Return a single Hatrick Straight meeting for the date.
    Use when fetch_meetings_for_date fails (e.g. timeout) so the venue still appears in the roster.
    """
    now_local = datetime.now().astimezone()
    return _hatrick_straight_fallback(meeting_date, now_local)


def _hatrick_straight_fallback(meeting_date: date, now_local: datetime) -> Meeting:
    """Always-available NZ greyhound meeting so Hatrick Straight appears even when GRNZ page fails."""
    return Meeting(
        code="greyhound",
        source="grnz_nz",
        venue="Hatrick Straight",
        meeting_date=meeting_date,
        first_race_time_local=time(11, 0),  # placeholder
        num_races=None,
        meeting_url=FIELDS_URL,
        status=_meeting_status(now_local, meeting_date, time(11, 0), None),
        extra={"country": "NZ"},
    )


def fetch_meetings_for_date(meeting_date: date, *, ttl_seconds: int = 300) -> list[Meeting]:
    """
    Parse GRNZ fields or calendar page for meetings on the given date.
    Returns list of Meeting (venue, meeting_url, etc.) so Hatrick Straight and other NZ tracks appear.
    If parsing fails or returns nothing, always includes Hatrick Straight for the date.
    """
    now_local = datetime.now().astimezone()
    meetings: list[Meeting] = []
    date_formats = _date_formats_for_match(meeting_date)

    for url in (FIELDS_URL, CALENDAR_URL):
        try:
            resp = get(url, ttl_seconds=ttl_seconds, timeout_seconds=30)
        except Exception:
            continue
        soup = BeautifulSoup(resp.text, "html.parser")
        page_text = (soup.get_text() or "") + " " + " ".join(
            (a.get("href") or "") for a in soup.find_all("a", href=True)
        )

        for a in soup.find_all("a", href=True):
            href = (a.get("href") or "").strip()
            text = (a.get_text(" ", strip=True) or "").strip()
            if not href or not text:
                continue
            full_url = urljoin(BASE, href)
            # Match if link or page contains any of our date formats
            date_found = any(fmt in href or fmt in full_url or fmt in page_text for fmt in date_formats)
            # Or link text looks like a venue (e.g. "Hatrick Straight") and we're on a fields/calendar page
            venue_like = ("hatrick" in text.lower() or "manukau" in text.lower() or "addington" in text.lower())
            if date_found or (venue_like and ("fields" in url or "calendar" in url)):
                venue = text.split("|")[0].strip() or _venue_from_url(href)
                if len(venue) < 2:
                    venue = _venue_from_url(href)
                if not venue and not venue_like:
                    continue
                if not venue and venue_like:
                    venue = text.split("|")[0].strip() or "Hatrick Straight"
                first_time = _first_time_from_text(text)
                meetings.append(
                    Meeting(
                        code="greyhound",
                        source="grnz_nz",
                        venue=venue,
                        meeting_date=meeting_date,
                        first_race_time_local=first_time,
                        num_races=None,
                        meeting_url=full_url if full_url.startswith("http") else FIELDS_URL,
                        status=_meeting_status(now_local, meeting_date, first_time, None),
                        extra={"country": "NZ"},
                    )
                )
            # Fallback: URL path suggests a venue (e.g. hatrick-straight)
            elif "/fields" in href.lower() or "/racing" in href.lower():
                slug = href.split("/")[-1].split("?")[0].replace("-", " ").replace("_", " ")
                if slug and len(slug) > 2 and slug not in ("fields.aspx", "calendar.aspx", "racing"):
                    if any(fmt in full_url for fmt in date_formats):
                        venue = text.split("|")[0].strip() or slug.title()
                        first_time = _first_time_from_text(text)
                        meetings.append(
                            Meeting(
                                code="greyhound",
                                source="grnz_nz",
                                venue=venue,
                                meeting_date=meeting_date,
                                first_race_time_local=first_time,
                                num_races=None,
                                meeting_url=full_url,
                                status=_meeting_status(now_local, meeting_date, first_time, None),
                                extra={"country": "NZ"},
                            )
                        )

        if meetings:
            break

    # Deduplicate by meeting_url
    seen: set[str] = set()
    out: list[Meeting] = []
    for m in meetings:
        if m.meeting_url in seen:
            continue
        seen.add(m.meeting_url)
        out.append(m)
    # If we got nothing from the page (timeout, different structure, or no date match), always show Hatrick Straight
    if not out:
        out.append(_hatrick_straight_fallback(meeting_date, now_local))
    out.sort(key=lambda m: (m.first_race_time_local or time(23, 59), m.venue))
    return out


def _venue_from_url(href: str) -> str:
    """Extract venue-like name from URL path (e.g. hatrick-straight -> Hatrick Straight)."""
    parts = href.replace("?", "/").split("/")
    for p in reversed(parts):
        p = p.strip()
        if not p or p in ("fields.aspx", "calendar.aspx", "racing", "catch-the-action", "aspx"):
            continue
        if _DATE_SLUG_RE.match(p):
            continue
        return p.replace("-", " ").replace("_", " ").title()
    return ""


def _first_time_from_text(text: str) -> Optional[time]:
    m = _TIME_RE.search(text)
    if not m:
        return None
    try:
        h, mm = int(m.group(1)), int(m.group(2))
        if 0 <= h <= 23 and 0 <= mm <= 59:
            return time(hour=h, minute=mm)
    except (ValueError, IndexError):
        pass
    return None


# Typical NZ greyhound meeting has ~8–12 races; placeholder so R8 etc. appear in roster
_DEFAULT_NUM_RACES = 12


def _placeholder_races(meeting_url: str) -> list[Race]:
    base = (meeting_url or "").rstrip("/")
    return [
        Race(
            code="greyhound",
            race_no=n,
            name=f"Race {n}",
            distance_m=None,
            start_time_local=None,
            race_url=f"{base}/{n}" if base else None,
            extra={},
        )
        for n in range(1, _DEFAULT_NUM_RACES + 1)
    ]


def _runners_from_cells(
    rows: list[list[str]], headers_norm: list[str]
) -> list[Runner]:
    """Parse rows of cells (with header indices) into Runner list. Shared by table and race-column paths."""
    def col_idx(*needles: str) -> Optional[int]:
        for i, h in enumerate(headers_norm):
            for n in needles:
                if n in h:
                    return i
        return None

    idx_box = col_idx("box", "trap", "no")
    idx_name = col_idx("dog", "runner", "greyhound", "name")
    idx_form = col_idx("last", "form")
    idx_split = col_idx("split", "first", "early", "1st", "av 1")

    def clean_name(s: str) -> str:
        s = re.sub(r"\s+", " ", (s or "")).strip()
        for sep in [" NBT", " T:", "R/T:", "R/T", "Trainer:"]:
            if sep in s:
                s = s.split(sep, 1)[0].strip()
        s = re.sub(r"\s+\d{1,2}\.\d{1,3}$", "", s).strip()
        return s

    runners: list[Runner] = []
    for cells in rows:
        def safe_get(i: Optional[int]) -> str:
            if i is None:
                return ""
            if 0 <= i < len(cells):
                return cells[i]
            return ""

        raw_name = safe_get(idx_name)
        if not raw_name:
            raw_name = max(cells, key=lambda s: len(s), default="")
        dog_name = clean_name(raw_name)
        if not dog_name or dog_name.lower() in {"scratched", "vacant", "res"}:
            continue

        raw_box = safe_get(idx_box)
        box = None
        m_box = re.search(r"\b(\d{1,2})\b", raw_box)
        if m_box:
            try:
                v = int(m_box.group(1))
                if 1 <= v <= 12:
                    box = v
            except Exception:
                pass
        if box is None and cells:
            m0 = re.fullmatch(r"\s*(\d{1,2})\s*", (cells[0] or ""))
            if m0:
                try:
                    v = int(m0.group(1))
                    if 1 <= v <= 12:
                        box = v
                except Exception:
                    pass

        form_text = safe_get(idx_form) or " ".join(cells)
        recent_finishes: list[int] = []
        m_seq = re.search(r"(\d{1,2}(?:\s*[-/,\s]\s*\d{1,2}){2,})", form_text)
        if m_seq:
            for n in re.findall(r"\d{1,2}", m_seq.group(1))[:5]:
                try:
                    v = int(n)
                    if 1 <= v <= 12:
                        recent_finishes.append(v)
                except Exception:
                    pass

        early_speed = None
        raw_split = safe_get(idx_split) or " ".join(cells)
        m_float = re.search(r"\b(\d{1,2}\.\d{1,3})\b", raw_split)
        if m_float:
            try:
                v = float(m_float.group(1))
                if 3.0 <= v <= 10.0:
                    early_speed = v
            except Exception:
                pass

        scratched = "scr" in (raw_name or "").lower() or "scr" in (raw_box or "").lower()
        runners.append(
            Runner(
                code="greyhound",
                name=dog_name,
                draw=box,
                recent_finishes=recent_finishes,
                early_speed=early_speed,
                scratched=scratched,
                raw={"cells": cells},
            )
        )
    return runners


def _parse_runner_table(tbl, race_no: int) -> list[Runner]:
    """Parse a table that looks like a greyhound field (Box/Dog/Runner/Form). Returns list of Runner."""
    thead = tbl.find("thead")
    headers_norm: list[str] = []
    if thead:
        headers_norm = [th.get_text(" ", strip=True).lower() for th in thead.find_all(["th", "td"])]
    row_nodes = tbl.find_all("tr")
    rows: list[list[str]] = []
    for tr in row_nodes:
        if tr.find("thead"):
            continue
        tds = tr.find_all(["td", "th"])
        if not tds:
            continue
        rows.append([td.get_text(" ", strip=True) for td in tds])
    return _runners_from_cells(rows, headers_norm)


def _parse_races_and_runners_from_page(soup: BeautifulSoup, meeting_url: str) -> tuple[list[Race], dict[int, list[Runner]]]:
    """Find runner-like tables and group by race. Returns (races, runners_by_race)."""
    races_out: list[Race] = []
    runners_by_race: dict[int, list[Runner]] = {}
    base = (meeting_url or "").rstrip("/")

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
        if any("race" in h for h in headers):
            score += 1
        score += min(len(tbl.find_all("tr")), 20) // 3
        return score

    def idx_race(headers_norm: list[str]) -> Optional[int]:
        for i, h in enumerate(headers_norm):
            if "race" in h and ("no" in h or "num" in h or "number" in h or h.strip() == "race"):
                return i
        for i, h in enumerate(headers_norm):
            if "race" in h:
                return i
        return None

    tables = soup.find_all("table")
    runner_tables: list[tuple[int, list[Runner]]] = []

    for tbl in tables:
        if score_table(tbl) < 2:
            continue
        thead = tbl.find("thead")
        headers_norm = []
        if thead:
            headers_norm = [th.get_text(" ", strip=True).lower() for th in thead.find_all(["th", "td"])]
        race_col = idx_race(headers_norm)

        if race_col is not None:
            # Single table with Race column: group rows by race number
            row_nodes = tbl.find_all("tr")
            by_race: dict[int, list[list[str]]] = {}
            for tr in row_nodes:
                if tr.find("thead"):
                    continue
                tds = tr.find_all(["td", "th"])
                if not tds:
                    continue
                cells = [td.get_text(" ", strip=True) for td in tds]
                if race_col < len(cells):
                    rn_str = cells[race_col].strip()
                    m_rn = re.search(r"\b(\d{1,2})\b", rn_str)
                    if m_rn:
                        try:
                            rn = int(m_rn.group(1))
                            if 1 <= rn <= 12:
                                by_race.setdefault(rn, []).append(cells)
                        except Exception:
                            pass
            for rn in sorted(by_race.keys()):
                # Build a minimal table-like structure for _parse_runner_table
                rows = by_race[rn]
                if 2 <= len(rows) <= 12:
                    runners = _runners_from_cells(rows, headers_norm)
                    if runners:
                        runner_tables.append((rn, runners))
            if runner_tables:
                break
        else:
            runners = _parse_runner_table(tbl, 1)
            if len(runners) >= 2 and len(runners) <= 12:
                runner_tables.append((len(runner_tables) + 1, runners))

    if not runner_tables:
        return ([], {})

    for race_no, runners in runner_tables:
        races_out.append(
            Race(
                code="greyhound",
                race_no=race_no,
                name=f"Race {race_no}",
                distance_m=None,
                start_time_local=None,
                race_url=f"{base}/{race_no}" if base else None,
                extra={},
            )
        )
        runners_by_race[race_no] = runners

    # Ensure R1..R12 exist for roster; fill missing with placeholder race + empty runners
    for n in range(1, _DEFAULT_NUM_RACES + 1):
        if n not in runners_by_race:
            runners_by_race[n] = []
        if not any(r.race_no == n for r in races_out):
            races_out.append(
                Race(
                    code="greyhound",
                    race_no=n,
                    name=f"Race {n}",
                    distance_m=None,
                    start_time_local=None,
                    race_url=f"{base}/{n}" if base else None,
                    extra={},
                )
            )
    races_out.sort(key=lambda r: r.race_no)
    return (races_out, runners_by_race)


def fetch_races_and_runners_for_meeting(
    meeting_url: str, meeting_date: date, *, ttl_seconds: int = 300
) -> tuple[list[Race], dict[int, list[Runner]]]:
    """
    Fetch GRNZ fields page and parse races + runners. On failure or no data, returns
    placeholder races (R1..R12) and empty runners_by_race so roster still shows the meeting.
    """
    urls_to_try = [
        meeting_url,
        FIELDS_URL,
        FIELDS_ERTS_URL,
        f"{FIELDS_URL}?date={meeting_date.strftime('%d/%m/%Y')}",
        f"{FIELDS_ERTS_URL}?date={meeting_date.strftime('%d/%m/%Y')}",
    ]
    for url in urls_to_try:
        if not url or not url.startswith("http"):
            continue
        try:
            resp = get(url, ttl_seconds=ttl_seconds, timeout_seconds=25)
            soup = BeautifulSoup(resp.text, "html.parser")
            races, runners_by_race = _parse_races_and_runners_from_page(soup, meeting_url or url)
            if races and runners_by_race:
                return (races, runners_by_race)
        except Exception:
            continue

    races = _placeholder_races(meeting_url)
    runners_by_race = {n: [] for n in range(1, _DEFAULT_NUM_RACES + 1)}
    return (races, runners_by_race)


def fetch_races_for_meeting(meeting_url: str, meeting_date: date, *, ttl_seconds: int = 300) -> list[Race]:
    """
    Return races for meeting. Prefer real data from fetch_races_and_runners_for_meeting;
    otherwise placeholder R1..R12.
    """
    races, _ = fetch_races_and_runners_for_meeting(meeting_url, meeting_date, ttl_seconds=ttl_seconds)
    return races
