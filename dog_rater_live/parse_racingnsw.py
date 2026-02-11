from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from typing import Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from fetch import FetchError, get
from models import Meeting, Race, Runner


BASE = "https://mdata.racingnsw.com.au"
TODAYS_RACING_URL = "https://mdata.racingnsw.com.au/FreeFields/todays_racing.aspx"


class ParseError(RuntimeError):
    pass


_TIME_12H_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})(AM|PM)\s*$", re.IGNORECASE)


def _parse_time_12h(s: str) -> Optional[time]:
    m = _TIME_12H_RE.match((s or "").strip())
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
    races = num_races or 8
    # Thoroughbred races typically ~30min apart-ish; keep rough.
    est_end = start_dt + timedelta(minutes=races * 35)
    if now_local <= est_end:
        return "in_progress"
    return "finished"


def fetch_meetings_for_date(meeting_date: date, *, ttl_seconds: int = 120) -> list[Meeting]:
    """
    v0: Racing NSW has a dedicated 'today' page, so for now we support:
    - chosen date == today => parse
    - other dates => []
    """
    if meeting_date != date.today():
        return []

    resp = get(TODAYS_RACING_URL, ttl_seconds=ttl_seconds)
    soup = BeautifulSoup(resp.text, "html.parser")

    meetings: list[Meeting] = []
    now_local = datetime.now().astimezone()

    for a in soup.find_all("a"):
        href = a.get("href") or ""
        if "StageMeeting.aspx?key=" not in href:
            continue
        venue = (a.get_text(" ", strip=True) or "").strip()
        if not venue:
            continue

        meeting_url = urljoin(BASE, href)
        meetings.append(
            Meeting(
                code="thoroughbred",
                source="racingnsw",
                venue=venue,
                meeting_date=meeting_date,
                first_race_time_local=None,
                num_races=None,
                meeting_url=meeting_url,
                status="unknown",
            )
        )

    # Dedup by URL
    uniq = {}
    for m in meetings:
        uniq[m.meeting_url] = m
    return sorted(uniq.values(), key=lambda m: m.venue)


def _acceptances_url_from_stage(meeting_url: str) -> str:
    """
    StageMeeting.aspx?key=YYYYMonDD,NSW,Venue -> Acceptances.aspx?Key=...
    """
    parsed = urlparse(meeting_url)
    q = parsed.query or ""
    m = re.search(r"(?:^|&)key=([^&]+)", q, re.IGNORECASE)
    if not m:
        # some pages use Key= in other casing
        m = re.search(r"(?:^|&)Key=([^&]+)", q, re.IGNORECASE)
    if not m:
        raise ParseError("Could not find meeting 'key' parameter in StageMeeting URL.")
    key_val = m.group(1)
    return f"{BASE}/FreeFields/Acceptances.aspx?Key={key_val}"


def fetch_races_and_runners_for_meeting(
    meeting_url: str, *, ttl_seconds: int = 120
) -> tuple[list[Race], dict[int, list[Runner]], dict]:
    """
    Parse the Acceptances page for a meeting and return:
    - list of Race objects
    - dict race_no -> list[Runner]
    """
    accept_url = _acceptances_url_from_stage(meeting_url)
    resp = get(accept_url, ttl_seconds=ttl_seconds)
    soup = BeautifulSoup(resp.text, "html.parser")

    # Races appear as text headings like:
    # "Race 1 - 1:35PM ... (1000 METRES)"
    # followed by a table with columns like:
    # No, Last 10, Horse, Trainer, Jockey, Barrier, Wgt(kg), B'mark, ...
    races: list[Race] = []
    runners_by_race: dict[int, list[Runner]] = {}

    # Meeting-level conditions (best-effort)
    page_text = soup.get_text(" ", strip=True)
    track_condition = None
    weather = None
    penetrometer = None
    m_cond = re.search(r"Track Condition:\s*([^W]+?)\s+Weather:", page_text, re.IGNORECASE)
    if m_cond:
        track_condition = m_cond.group(1).strip()
    m_weather = re.search(r"Weather:\s*([^P]+?)\s+Penetrometer:", page_text, re.IGNORECASE)
    if m_weather:
        weather = m_weather.group(1).strip()
    m_pen = re.search(r"Penetrometer:\s*([0-9.]+)", page_text, re.IGNORECASE)
    if m_pen:
        penetrometer = m_pen.group(1).strip()

    # Strategy: walk elements; when we see a "Race X - TIME" heading, set current_race,
    # then take the next table as runner table.
    heading_nodes = []
    for el in soup.find_all(["h1", "h2", "h3", "h4", "div", "p"]):
        txt = (el.get_text(" ", strip=True) or "").strip()
        if txt.lower().startswith("race "):
            heading_nodes.append(el)

    for el in heading_nodes:
        txt = (el.get_text(" ", strip=True) or "").strip()
        m = re.match(r"Race\s+(\d+)\s*-\s*([0-9:]+(?:AM|PM))\s+(.*)$", txt, re.IGNORECASE)
        if not m:
            continue
        race_no = int(m.group(1))
        start_t = _parse_time_12h(m.group(2))
        rest = (m.group(3) or "").strip()
        dist_m = None
        m_dist = re.search(r"\((\d{3,4})\s*METRES\)", rest, re.IGNORECASE)
        if m_dist:
            try:
                dist_m = int(m_dist.group(1))
            except Exception:
                dist_m = None

        races.append(
            Race(
                code="thoroughbred",
                race_no=race_no,
                name=f"Race {race_no}",
                distance_m=dist_m,
                start_time_local=start_t,
                race_url=accept_url + f"#race{race_no}",
                extra={"heading": txt},
            )
        )

        tbl = el.find_next("table")
        if tbl is None:
            continue

        # parse headers -> idx
        thead = tbl.find("thead")
        headers = [th.get_text(" ", strip=True).strip() for th in thead.find_all(["th", "td"])] if thead else []
        hn = [h.lower() for h in headers]

        def idx_contains(*needles: str) -> Optional[int]:
            for i, h in enumerate(hn):
                for n in needles:
                    if n in h:
                        return i
            return None

        idx_horse = idx_contains("horse")
        idx_barrier = idx_contains("barrier")
        idx_wgt = idx_contains("wgt")
        idx_bm = idx_contains("b'mark", "bmark", "bench")
        idx_last10 = idx_contains("last 10", "last10")
        idx_trainer = idx_contains("trainer")
        idx_jockey = idx_contains("jockey")

        tbody = tbl.find("tbody") or tbl
        rs: list[Runner] = []
        for tr in tbody.find_all("tr"):
            tds = tr.find_all(["td", "th"])
            if not tds:
                continue
            cells = [td.get_text(" ", strip=True) for td in tds]

            profile_url = None
            if idx_horse is not None and 0 <= idx_horse < len(tds):
                a = tds[idx_horse].find("a")
                if a and a.get("href"):
                    profile_url = urljoin(BASE, a.get("href"))

            def safe(i: Optional[int]) -> str:
                if i is None:
                    return ""
                if 0 <= i < len(cells):
                    return cells[i]
                return ""

            horse = safe(idx_horse).strip()
            if not horse:
                continue
            horse = re.sub(r"\s+", " ", horse)

            barrier = None
            btxt = safe(idx_barrier)
            mb = re.search(r"\b(\d{1,2})\b", btxt)
            if mb:
                try:
                    barrier = int(mb.group(1))
                except Exception:
                    barrier = None

            weight_kg = None
            wtxt = safe(idx_wgt)
            mw = re.search(r"\b(\d{2}\.?\d?)\b", wtxt)
            if mw:
                try:
                    weight_kg = float(mw.group(1))
                except Exception:
                    weight_kg = None

            bm = None
            bmtxt = safe(idx_bm)
            mbm = re.search(r"\b(\d{1,3})\b", bmtxt)
            if mbm:
                try:
                    bm = float(mbm.group(1))
                except Exception:
                    bm = None

            last10 = safe(idx_last10).strip() or None

            # derive recent finishes from last10 like "13x3112260"
            recent: list[int] = []
            if last10:
                # digits are finishes; 0 means 10th+ (treat as 10)
                for ch in re.findall(r"[0-9xX]", last10):
                    if ch.lower() == "x":
                        continue
                    v = int(ch)
                    recent.append(10 if v == 0 else v)

            trainer = safe(idx_trainer).strip() or None
            jockey = safe(idx_jockey).strip() or None

            rs.append(
                Runner(
                    code="thoroughbred",
                    name=horse,
                    draw=barrier,
                    recent_finishes=recent[:5],
                    early_speed=None,
                    profile_url=profile_url,
                    weight_kg=weight_kg,
                    benchmark=bm,
                    trainer=trainer,
                    jockey_or_driver=jockey,
                    last10=last10,
                    scratched=("SCR" in " ".join(cells).upper()),
                    raw={"headers": headers, "cells": cells, "acceptances_url": accept_url},
                )
            )

        runners_by_race[race_no] = [r for r in rs if not r.scratched]

    races.sort(key=lambda r: r.race_no)
    meeting_meta = {"track_condition": track_condition, "weather": weather, "penetrometer": penetrometer}
    return races, runners_by_race, meeting_meta


def enrich_meeting_with_first_race(meeting: Meeting, races: list[Race]) -> Meeting:
    first_time = None
    if races:
        first_time = races[0].start_time_local
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

