from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Optional
from urllib.parse import parse_qs, urlparse, urljoin

from bs4 import BeautifulSoup

from fetch import get
from parse_thedogs import fetch_races_for_meeting


@dataclass(frozen=True)
class RaceResult:
    """Winner and optional top 3 placings (1st, 2nd, 3rd) for compression backtest."""
    race_no: int
    winner: Optional[str]
    source_url: str
    places: tuple[str, ...] = ()  # (1st, 2nd, 3rd) when available


def _clean_name_basic(s: str) -> str:
    s = re.sub(r"\s+", " ", (s or "")).strip()
    # strip common trailing metadata chunks
    for sep in [" NBT", " T:", "R/T:", "Trainer:"]:
        if sep in s:
            s = s.split(sep, 1)[0].strip()
    return s


def fetch_greyhound_winner_for_race(race_url: str) -> Optional[str]:
    html = get(race_url, ttl_seconds=60, timeout_seconds=25).text
    soup = BeautifulSoup(html, "html.parser")

    # Look for an obvious placings/result table: first cell "1st" (or "1")
    for tr in soup.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
        if len(cells) < 2:
            continue
        place = (cells[0] or "").strip().lower()
        if place not in {"1st", "1"}:
            continue
        # pick first non-numeric-ish cell after place as the name
        name = None
        for c in cells[1:]:
            c2 = (c or "").strip()
            if not c2:
                continue
            if re.fullmatch(r"[0-9.]+", c2):
                continue
            if len(c2) < 2:
                continue
            if re.search(r"[A-Za-z]", c2):
                name = c2
                break
        if not name:
            # fallback: longest alpha-containing cell
            alpha = [c for c in cells[1:] if re.search(r"[A-Za-z]", c or "")]
            if alpha:
                name = max(alpha, key=lambda x: len(x or ""))
        if name:
            return _clean_name_basic(name)

    return None


def fetch_greyhound_results_for_meeting(meeting_url: str) -> dict[int, RaceResult]:
    out: dict[int, RaceResult] = {}
    races = fetch_races_for_meeting(meeting_url, ttl_seconds=120)
    for r in races:
        w = fetch_greyhound_winner_for_race(r.race_url)
        out[r.race_no] = RaceResult(race_no=r.race_no, winner=w, source_url=r.race_url)
    return out


def _racingnsw_results_url_from_stage(meeting_url: str) -> str:
    parsed = urlparse(meeting_url)
    qs = parse_qs(parsed.query)
    key = (qs.get("key") or qs.get("Key") or [""])[0]
    if not key:
        raise ValueError("Missing key param for RacingNSW meeting URL.")
    return f"https://mdata.racingnsw.com.au/FreeFields/Results.aspx?Key={key}"


def fetch_racingnsw_results_for_meeting(meeting_url: str) -> dict[int, RaceResult]:
    url = _racingnsw_results_url_from_stage(meeting_url)
    html = get(url, ttl_seconds=60, timeout_seconds=30).text
    soup = BeautifulSoup(html, "html.parser")

    out: dict[int, RaceResult] = {}
    current_race: Optional[int] = None

    def _finish_to_rank(fin: str) -> Optional[int]:
        fin = (fin or "").strip().lower()
        if fin in {"1", "1st"}:
            return 1
        if fin in {"2", "2nd"}:
            return 2
        if fin in {"3", "3rd"}:
            return 3
        return None

    def parse_table_for_placings(tbl) -> tuple[Optional[str], tuple[str, ...]]:
        """Return (winner, (1st, 2nd, 3rd)) for compression backtest."""
        thead = tbl.find("thead")
        headers = []
        if thead:
            headers = [th.get_text(" ", strip=True).strip() for th in thead.find_all(["th", "td"])]
        else:
            first_tr = tbl.find("tr")
            if first_tr:
                headers = [th.get_text(" ", strip=True).strip() for th in first_tr.find_all(["th", "td"])]
        hn = [h.lower() for h in headers]
        try:
            idx_finish = hn.index("finish")
        except ValueError:
            idx_finish = next((i for i, h in enumerate(hn) if "finish" in h or "pos" in h), None)
        idx_horse = next((i for i, h in enumerate(hn) if h == "horse" or "horse" in h), None)
        if idx_finish is None or idx_horse is None:
            return (None, ())

        rows: list[tuple[int, str]] = []
        tbody = tbl.find("tbody") or tbl
        for tr in tbody.find_all("tr"):
            tds = tr.find_all(["td", "th"])
            if not tds:
                continue
            cells = [td.get_text(" ", strip=True) for td in tds]
            if idx_finish >= len(cells) or idx_horse >= len(cells):
                continue
            fin = (cells[idx_finish] or "").strip()
            rank = _finish_to_rank(fin)
            if rank is None:
                continue
            name = (cells[idx_horse] or "").strip()
            if name:
                rows.append((rank, _clean_name_basic(name)))
        rows.sort(key=lambda x: x[0])
        top3 = [name for _, name in rows[:3]]
        winner = top3[0] if top3 else None
        places = tuple(top3) if top3 else ()
        return (winner, places)

    # Walk in DOM order; update current_race from nearby headings, then parse following tables.
    for el in soup.find_all(True):
        txt = (el.get_text(" ", strip=True) or "").strip()
        m = re.match(r"^Race\s+(\d+)\b", txt, re.IGNORECASE)
        if m:
            try:
                current_race = int(m.group(1))
            except Exception:
                current_race = None
            continue
        if el.name != "table" or current_race is None:
            continue
        winner, places = parse_table_for_placings(el)
        if winner is not None and current_race not in out:
            out[current_race] = RaceResult(race_no=current_race, winner=winner, source_url=url, places=places)

    return out


def _harness_results_url_from_form(meeting_url: str) -> str:
    parsed = urlparse(meeting_url)
    qs = parse_qs(parsed.query)
    mc = (qs.get("mc") or [""])[0]
    if not mc:
        raise ValueError("Missing mc param for harness meeting URL.")
    # NSW meetings are under ms=NSW
    return f"https://www.harness.org.au/meeting-results.cfm?mc={mc}&ms=NSW"


def fetch_harness_results_for_meeting(meeting_url: str) -> dict[int, RaceResult]:
    url = _harness_results_url_from_form(meeting_url)
    html = get(url, ttl_seconds=60, timeout_seconds=30).text
    soup = BeautifulSoup(html, "html.parser")

    out: dict[int, RaceResult] = {}
    current_race: Optional[int] = None

    # Similar strategy: track "Race N" headings; then parse next result table rows.
    for el in soup.find_all(True):
        txt = (el.get_text(" ", strip=True) or "").strip()
        m = re.match(r"^Race\s+(\d+)\b", txt, re.IGNORECASE)
        if m:
            try:
                current_race = int(m.group(1))
            except Exception:
                current_race = None
            continue

        if el.name != "table" or current_race is None or current_race in out:
            continue

        # Find a table that has a "Pl" / "Place" / "Finish" and "Horse" column.
        thead = el.find("thead")
        headers = []
        if thead:
            headers = [th.get_text(" ", strip=True).strip() for th in thead.find_all(["th", "td"])]
        else:
            first_tr = el.find("tr")
            if first_tr:
                headers = [th.get_text(" ", strip=True).strip() for th in first_tr.find_all(["th", "td"])]
        hn = [h.lower() for h in headers]
        idx_fin = next((i for i, h in enumerate(hn) if h in {"pl", "place", "finish"} or "place" in h or "finish" in h), None)
        idx_name = next((i for i, h in enumerate(hn) if "horse" in h or "runner" in h), None)
        if idx_fin is None or idx_name is None:
            continue

        tbody = el.find("tbody") or el
        for tr in tbody.find_all("tr"):
            tds = tr.find_all(["td", "th"])
            if not tds:
                continue
            cells = [td.get_text(" ", strip=True) for td in tds]
            if idx_fin >= len(cells) or idx_name >= len(cells):
                continue
            fin = (cells[idx_fin] or "").strip().lower()
            if fin not in {"1", "1st"}:
                continue
            name = _clean_name_basic(cells[idx_name] or "")
            if name:
                out[current_race] = RaceResult(race_no=current_race, winner=name, source_url=url)
                break

    return out


def fetch_results_for_meeting(code: str, meeting_url: str) -> dict[int, RaceResult]:
    if code == "greyhound":
        return fetch_greyhound_results_for_meeting(meeting_url)
    if code == "thoroughbred":
        return fetch_racingnsw_results_for_meeting(meeting_url)
    if code == "harness":
        return fetch_harness_results_for_meeting(meeting_url)
    return {}


def fetch_results_for_date(
    *,
    d: date,
    code: str,
    meetings: list,
) -> dict[str, dict[int, RaceResult]]:
    """
    Return mapping meeting_url -> {race_no -> RaceResult}.
    """
    out: dict[str, dict[int, RaceResult]] = {}
    for m in meetings:
        try:
            out[m.meeting_url] = fetch_results_for_meeting(code, m.meeting_url)
        except Exception:
            out[m.meeting_url] = {}
    return out

