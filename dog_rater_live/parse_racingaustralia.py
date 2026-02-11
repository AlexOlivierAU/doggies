from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from typing import Optional
from urllib.parse import urljoin, urlparse, parse_qs

from bs4 import BeautifulSoup

from fetch import FetchError, get
from models import Meeting, Race, Runner


BASE = "https://www.racingaustralia.horse"
CAL_URL = "https://www.racingaustralia.horse/FreeFields/Calendar.aspx?State={state}"

# Key shape: 2025Dec06,WA,Ascot
_KEY_RE = re.compile(r"^\s*(\d{4})([A-Za-z]{3})(\d{2})\s*$")
_TIME_12H_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})(AM|PM)\s*$", re.IGNORECASE)
_RACE_LINE_RE = re.compile(
    r"Race\s+(?P<no>\d+)\s*-\s*(?P<t>\d{1,2}:\d{2}\s*(?:AM|PM))\s+(?P<name>.+?)\s*\((?P<dist>\d{3,4})\s*METRE",
    re.IGNORECASE,
)


class ParseError(RuntimeError):
    pass


def _parse_key_date(s: str) -> Optional[date]:
    m = _KEY_RE.match((s or "").strip())
    if not m:
        return None
    y = int(m.group(1))
    mon = m.group(2).lower()
    d = int(m.group(3))
    months = {
        "jan": 1,
        "feb": 2,
        "mar": 3,
        "apr": 4,
        "may": 5,
        "jun": 6,
        "jul": 7,
        "aug": 8,
        "sep": 9,
        "oct": 10,
        "nov": 11,
        "dec": 12,
    }
    if mon not in months:
        return None
    return date(y, months[mon], d)


def _parse_time_12h(s: str) -> Optional[time]:
    m = _TIME_12H_RE.match((s or "").strip().replace(" ", ""))
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
    est_end = start_dt + timedelta(minutes=races * 35)
    if now_local <= est_end:
        return "in_progress"
    return "finished"


def _key_from_url(url: str) -> Optional[str]:
    try:
        q = parse_qs(urlparse(url).query)
    except Exception:
        return None
    k = (q.get("Key") or [None])[0]
    return k


def fetch_meetings_for_date(meeting_date: date, *, ttl_seconds: int = 300) -> list[Meeting]:
    """
    Fetch *all Australian* thoroughbred meetings for a given date using Racing Australia FreeFields.
    We scrape state calendar pages and collect Acceptances links (Key=YYYYMonDD,STATE,VENUE).
    """
    states = ["NSW", "VIC", "QLD", "WA", "SA", "TAS", "ACT", "NT"]
    now_local = datetime.now().astimezone()

    # We prefer Acceptances when present (best runner detail), but some calendar rows
    # may not include acceptances links even when a meeting exists (e.g. only Weights is linked).
    # So we accept multiple page types and dedupe by meeting Key.
    best_by_key: dict[str, tuple[int, str, str, str]] = {}  # key -> (priority, url, venue, state)
    # priority: higher wins
    priorities = {
        "Acceptances.aspx?Key=": 4,
        "Form.aspx?Key=": 3,
        "AllForm.aspx?Key=": 3,
        "Weights.aspx?Key=": 2,
        "RaceProgram.aspx?Key=": 1,
        "Nominations.aspx?Key=": 0,
    }

    for st in states:
        url = CAL_URL.format(state=st)
        resp = get(url, ttl_seconds=ttl_seconds)
        soup = BeautifulSoup(resp.text, "html.parser")
        for a in soup.find_all("a"):
            href = a.get("href") or ""
            pr = None
            for frag, p in priorities.items():
                if frag in href:
                    pr = p
                    break
            if pr is None:
                continue
            full = urljoin(BASE, href)
            key = _key_from_url(full)
            if not key:
                continue
            parts = [p.strip() for p in key.split(",") if p.strip()]
            if len(parts) < 3:
                continue
            d = _parse_key_date(parts[0])
            if d != meeting_date:
                continue
            venue = parts[2].strip()
            state = parts[1].strip()
            cur = best_by_key.get(key)
            if cur is None or pr > cur[0]:
                best_by_key[key] = (pr, full, venue, state)

    meetings: list[Meeting] = []
    for key, (_pr, full, venue, state) in best_by_key.items():
        meetings.append(
            Meeting(
                code="thoroughbred",
                source="racingaustralia",
                venue=venue,
                meeting_date=meeting_date,
                first_race_time_local=None,
                num_races=None,
                meeting_url=full,
                status=_meeting_status(now_local, meeting_date, None, None),
                extra={"state": state, "key": key},
            )
        )
    return sorted(meetings, key=lambda m: (m.extra.get("state") or "", m.venue))


def fetch_races_and_runners_for_meeting(meeting_url: str, *, ttl_seconds: int = 300) -> tuple[list[Race], dict[int, list[Runner]], dict]:
    """
    Parse races + runners from a Racing Australia Acceptances page.
    """
    key = _key_from_url(meeting_url)
    if not key:
        raise ParseError("Missing Key=... in meeting URL")

    # Prefer Acceptances page for runners (has barrier/jockey etc), even if the calendar linked a different page.
    # Fall back to the provided meeting_url (often Weights) if acceptances is unavailable.
    acceptances_url = f"{BASE}/FreeFields/Acceptances.aspx?Key={key}"
    soup = None
    try:
        resp = get(acceptances_url, ttl_seconds=ttl_seconds)
        soup = BeautifulSoup(resp.text, "html.parser")
    except FetchError:
        resp2 = get(meeting_url, ttl_seconds=ttl_seconds)
        soup = BeautifulSoup(resp2.text, "html.parser")

    # Race program: has race times even when weights/acceptances layouts omit them.
    program_url = f"{BASE}/FreeFields/RaceProgram.aspx?Key={key}"
    program_meta: dict[int, dict] = {}
    try:
        presp = get(program_url, ttl_seconds=ttl_seconds)
        ptext = BeautifulSoup(presp.text, "html.parser").get_text(" ", strip=True)
        for m in _RACE_LINE_RE.finditer(ptext):
            try:
                rn = int(m.group("no"))
            except Exception:
                continue
            program_meta[rn] = {
                "name": (m.group("name") or "").strip(),
                "distance_m": int(m.group("dist")),
                "start_time_local": _parse_time_12h(m.group("t")),
            }
    except Exception:
        program_meta = {}

    # Meta (best-effort)
    page_text = soup.get_text("\n", strip=True)
    meta: dict[str, str] = {}
    for label in ["Track Condition", "Weather", "Penetrometer"]:
        m = re.search(rf"{re.escape(label)}:\s*([^\n]+)", page_text, re.IGNORECASE)
        if m:
            meta_key = label.lower().replace(" ", "_")
            meta[meta_key] = m.group(1).strip()

    races: list[Race] = []
    runners_by_race: dict[int, list[Runner]] = {}

    # Race anchors are typically named Race1, Race2, ...
    for race_no in range(1, 25):
        anchor = soup.find(attrs={"name": f"Race{race_no}"}) or soup.find(id=f"Race{race_no}")
        if anchor is None:
            # Stop if we fail early; otherwise keep going in case of gaps.
            if race_no == 1:
                continue
            break

        # Collect the section until the next race anchor.
        section_text = ""
        runner_table = None
        for el in anchor.next_elements:
            if getattr(el, "attrs", None):
                nm = el.attrs.get("name") or el.attrs.get("id")
                if isinstance(nm, str) and nm.startswith("Race") and nm != f"Race{race_no}":
                    break
            if getattr(el, "get_text", None):
                t = el.get_text(" ", strip=True)
                if t:
                    section_text += " " + t
            if getattr(el, "name", None) == "table" and runner_table is None:
                headers = [th.get_text(" ", strip=True) for th in el.find_all("th")]
                hlow = [h.strip().lower() for h in headers]
                # Acceptances pages: horse+barrier. Weights pages: horse+weight (no barrier).
                if "horse" in hlow and ("barrier" in hlow or "weight" in hlow or "true weight" in hlow):
                    runner_table = el
                    # Keep scanning a little more in case the race line appears after the table.

        # Prefer program meta; fall back to parsing the local section text.
        pm = program_meta.get(race_no) or {}
        m_race = _RACE_LINE_RE.search(section_text)
        race_name = (pm.get("name") or "").strip() or (m_race.group("name").strip() if m_race else f"Race {race_no}")
        dist_m = pm.get("distance_m")
        if dist_m is None and m_race:
            try:
                dist_m = int(m_race.group("dist"))
            except Exception:
                dist_m = None
        start_t = pm.get("start_time_local")
        if start_t is None and m_race:
            start_t = _parse_time_12h(m_race.group("t"))

        race_url = meeting_url.split("#", 1)[0] + f"#Race{race_no}"
        races.append(
            Race(
                code="thoroughbred",
                race_no=race_no,
                name=race_name,
                distance_m=dist_m,
                start_time_local=start_t,
                race_url=race_url,
            )
        )

        runners: list[Runner] = []
        if runner_table is not None:
            headers = [th.get_text(" ", strip=True) for th in runner_table.find_all("th")]
            hlow = [h.strip().lower() for h in headers]

            def idx(*names: str) -> Optional[int]:
                for nm in names:
                    nm = nm.lower()
                    if nm in hlow:
                        return hlow.index(nm)
                return None

            i_last10 = idx("last 10", "last10", "last 5")
            i_horse = idx("horse", "runner")
            i_sex = idx("sex")
            i_age = idx("age")
            i_trainer = idx("trainer")
            i_jockey = idx("jockey", "rider")
            i_barrier = idx("barrier")
            # Some pages (e.g. Weights) have no barrier; keep draw=None in that case.
            i_weight = idx("weight")
            i_hcp = idx("hcp rating", "rating", "handicap rating")

            for tr in runner_table.find_all("tr"):
                tds = tr.find_all("td")
                if not tds:
                    continue
                cells = [td.get_text(" ", strip=True) for td in tds]
                if i_horse is None or i_horse >= len(cells):
                    continue

                horse_name = cells[i_horse].strip()
                if not horse_name:
                    continue

                # profile link (horse full form)
                prof = None
                try:
                    a = tds[i_horse].find("a")
                    if a and a.get("href"):
                        prof = urljoin(meeting_url, a.get("href"))
                except Exception:
                    prof = None

                last10 = cells[i_last10].strip() if (i_last10 is not None and i_last10 < len(cells)) else ""
                finishes: list[int] = []
                for ch in last10:
                    if ch.isdigit():
                        v = int(ch)
                        finishes.append(10 if v == 0 else v)

                sex = cells[i_sex].strip() if (i_sex is not None and i_sex < len(cells)) else None
                age = None
                if i_age is not None and i_age < len(cells):
                    try:
                        age = int(re.sub(r"[^\d]", "", cells[i_age]) or "")
                    except Exception:
                        age = None
                # barrier
                draw = None
                if i_barrier is not None and i_barrier < len(cells):
                    try:
                        draw = int(re.sub(r"[^\d]", "", cells[i_barrier]) or "0") or None
                    except Exception:
                        draw = None
                # weight
                wt = None
                if i_weight is not None and i_weight < len(cells):
                    mwt = re.search(r"(\d+(?:\.\d+)?)\s*kg", cells[i_weight], re.IGNORECASE)
                    if mwt:
                        try:
                            wt = float(mwt.group(1))
                        except Exception:
                            wt = None
                # benchmark/rating
                bm = None
                if i_hcp is not None and i_hcp < len(cells):
                    try:
                        bm = float(re.sub(r"[^\d.]", "", cells[i_hcp]) or "")
                    except Exception:
                        bm = None

                trainer = cells[i_trainer].strip() if (i_trainer is not None and i_trainer < len(cells)) else None
                jockey = cells[i_jockey].strip() if (i_jockey is not None and i_jockey < len(cells)) else None

                runners.append(
                    Runner(
                        code="thoroughbred",
                        name=horse_name,
                        draw=draw,
                        recent_finishes=finishes,
                        early_speed=None,
                        age=age,
                        sex=sex,
                        profile_url=prof,
                        weight_kg=wt,
                        benchmark=bm,
                        trainer=trainer,
                        jockey_or_driver=jockey,
                        last10=last10 or None,
                        scratched=False,
                        raw={"headers": headers, "cells": cells},
                    )
                )

        runners_by_race[race_no] = runners

    # If acceptances page had no Race1-style anchors (e.g. Caulfield Heath layout change), build races from program only
    if not races and program_meta:
        for race_no in sorted(program_meta.keys()):
            pm = program_meta[race_no]
            race_name = (pm.get("name") or "").strip() or f"Race {race_no}"
            races.append(
                Race(
                    code="thoroughbred",
                    race_no=race_no,
                    name=race_name,
                    distance_m=pm.get("distance_m"),
                    start_time_local=pm.get("start_time_local"),
                    race_url=(meeting_url.split("#", 1)[0] + f"#Race{race_no}"),
                )
            )
            runners_by_race[race_no] = []

    if not races:
        raise ParseError("Could not find any races on acceptances page (layout may have changed).")

    # Try to infer meeting-level fields
    times = [r.start_time_local for r in races if isinstance(r.start_time_local, time)]
    first_t = min(times) if times else None

    # Update meta for conditions (used by UI)
    out_meta = {
        "track_condition": meta.get("track_condition"),
        "weather": meta.get("weather"),
        "penetrometer": meta.get("penetrometer"),
        "first_race_time_local": first_t.strftime("%H:%M") if isinstance(first_t, time) else None,
    }

    return races, runners_by_race, out_meta

