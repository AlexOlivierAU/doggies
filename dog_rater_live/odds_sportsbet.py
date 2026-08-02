"""
Best-effort win/place odds + flucs from Sportsbet's public racing API.

Not official TAB — same market-feel data AU punters use. Cached via fetch.py.
"""

from __future__ import annotations

import json
import re
from datetime import date
from typing import Any, Optional

from fetch import FetchError, get

BASE = "https://www.sportsbet.com.au/apigw/sportsbook-racing/Sportsbook/Racing"
_HEADERS = {
    "Accept": "application/json",
    "Referer": "https://www.sportsbet.com.au/racing",
    "Origin": "https://www.sportsbet.com.au",
}


def _norm_venue(name: str) -> str:
    s = (name or "").strip().lower()
    s = re.sub(r"\s*\([^)]*\)\s*$", "", s)
    s = re.sub(r"^bet365\s+", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    # Common Sportsbet vs Racing Australia naming drift
    aliases = {
        "thomas farms rc murray bridge": "murray bridge",
        "murray bridge": "murray bridge",
        "caulfield heath": "caulfield",
        "sportsbet sandown hillside": "sandown",
        "sportsbet sandown lakeside": "sandown",
        "sandown hills": "sandown",
        "sandown lakeside": "sandown",
    }
    return aliases.get(s, s)


def norm_horse_name(name: str) -> str:
    s = (name or "").strip()
    s = re.sub(r"\s*\(([A-Z]{2,3}|NZ|GB|IRE|USA|FR|JPN|GER|ITY)\)\s*$", "", s, flags=re.I)
    return re.sub(r"\s+", " ", s).strip().lower()


# Back-compat alias
_norm_horse = norm_horse_name


def _decimal_from_prices(prices: list[dict]) -> tuple[Optional[float], Optional[float]]:
    win = place = None
    for p in prices or []:
        if (p.get("priceCode") or "") != "L":
            continue
        try:
            if p.get("winPrice") is not None:
                win = float(p["winPrice"])
        except Exception:
            pass
        try:
            if p.get("placePrice") is not None:
                place = float(p["placePrice"])
        except Exception:
            pass
        break
    return win, place


def _fluc_arrow(flucs: list[float], current: Optional[float]) -> str:
    """↓ shortening (price down), ↑ drifting (price up), → steady."""
    xs = [float(x) for x in (flucs or []) if isinstance(x, (int, float))]
    if current is None and not xs:
        return ""
    cur = float(current) if current is not None else xs[-1]
    if len(xs) >= 2:
        prev = xs[0]
    elif len(xs) == 1:
        prev = xs[0]
    else:
        return ""
    if cur < prev - 0.05:
        return "↓"  # shortened
    if cur > prev + 0.05:
        return "↑"  # drifted
    return "→"


def fetch_all_racing(meeting_date: date, *, ttl_seconds: int = 90) -> dict:
    url = f"{BASE}/AllRacing/{meeting_date.isoformat()}"
    try:
        resp = get(url, ttl_seconds=ttl_seconds, timeout_seconds=25.0, headers=_HEADERS)
    except FetchError:
        return {}
    try:
        return json.loads(resp.text)
    except Exception:
        return {}


def list_au_tb_events(meeting_date: date, *, ttl_seconds: int = 90) -> list[dict[str, Any]]:
    """Flat list of AU thoroughbred events for the day."""
    data = fetch_all_racing(meeting_date, ttl_seconds=ttl_seconds)
    out: list[dict[str, Any]] = []
    for day in data.get("dates") or []:
        for sec in day.get("sections") or []:
            if (sec.get("raceType") or "") != "horse":
                continue
            for m in sec.get("meetings") or []:
                region = (m.get("regionName") or "").lower()
                if region and region not in {"australia", "aus/nz", "new zealand"}:
                    # Keep AU + NZ horses section; skip overseas
                    if "aust" not in region and "nz" not in region:
                        continue
                venue = m.get("name") or ""
                for e in m.get("events") or []:
                    out.append(
                        {
                            "event_id": e.get("id"),
                            "venue": venue,
                            "venue_key": _norm_venue(venue),
                            "race_no": e.get("raceNumber"),
                            "name": e.get("name") or "",
                            "status": e.get("statusCode") or e.get("bettingStatus") or "",
                            "start_time": e.get("startTime"),
                            "resulted": bool(e.get("result")) or (e.get("statusCode") == "R"),
                        }
                    )
    return out


def fetch_race_markets(event_id: int, *, ttl_seconds: int = 60) -> list[dict]:
    url = f"{BASE}/Events/{int(event_id)}/Markets"
    try:
        resp = get(url, ttl_seconds=ttl_seconds, timeout_seconds=25.0, headers=_HEADERS)
    except FetchError:
        return []
    try:
        data = json.loads(resp.text)
    except Exception:
        return []
    return data if isinstance(data, list) else []


def odds_by_horse_for_event(event_id: int, *, ttl_seconds: int = 60) -> dict[str, dict[str, Any]]:
    """
    horse_norm -> {name, no, win, place, fluc, flucs, scratched}
    Prefer the Win or Place market.
    """
    markets = fetch_race_markets(event_id, ttl_seconds=ttl_seconds)
    win_m = None
    for m in markets:
        name = (m.get("name") or "").lower()
        if "win or place" in name or name.strip() == "win":
            win_m = m
            break
    if win_m is None and markets:
        win_m = markets[0]
    if not win_m:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for sel in win_m.get("selections") or []:
        if not isinstance(sel, dict):
            continue
        name = (sel.get("name") or "").strip()
        if not name:
            continue
        win, place = _decimal_from_prices(sel.get("prices") or [])
        flucs = sel.get("recentOddsFluctuations") or []
        flucs_f = []
        for x in flucs:
            try:
                flucs_f.append(float(x))
            except Exception:
                pass
        key = _norm_horse(name)
        out[key] = {
            "name": name,
            "no": sel.get("runnerNumber"),
            "win": win,
            "place": place,
            "fluc": _fluc_arrow(flucs_f, win),
            "flucs": flucs_f[-8:],
            "scratched": bool(sel.get("isOut")),
        }
    return out


def build_event_index(meeting_date: date, *, ttl_seconds: int = 90) -> dict[tuple[str, int], int]:
    """(venue_key, race_no) -> event_id"""
    idx: dict[tuple[str, int], int] = {}
    for e in list_au_tb_events(meeting_date, ttl_seconds=ttl_seconds):
        eid = e.get("event_id")
        rn = e.get("race_no")
        vk = e.get("venue_key") or ""
        if eid is None or rn is None or not vk:
            continue
        try:
            idx[(vk, int(rn))] = int(eid)
        except Exception:
            continue
    return idx


def lookup_event_id(
    event_index: dict[tuple[str, int], int],
    venue: str,
    race_no: int,
) -> Optional[int]:
    vk = _norm_venue(venue)
    if (vk, int(race_no)) in event_index:
        return event_index[(vk, int(race_no))]
    # soft match: venue key contained / contains
    for (ev_v, rn), eid in event_index.items():
        if rn != int(race_no):
            continue
        if vk in ev_v or ev_v in vk:
            return eid
    return None


def format_odds_suffix(win: Optional[float], fluc: str = "") -> str:
    if win is None:
        return ""
    # Compact: 4.8 / 12 / 2.15
    if abs(win - round(win)) < 0.05:
        s = str(int(round(win)))
    else:
        s = f"{win:.2f}".rstrip("0").rstrip(".")
    return f" ${s}{fluc or ''}"


def sportsbet_race_url(event_id: int) -> str:
    return f"https://www.sportsbet.com.au/horse-racing/australia-nz/{int(event_id)}"


def collect_sportsbet_scratchings(
    meeting_date: date,
    *,
    max_events: int = 40,
    ttl_seconds: int = 90,
) -> list[dict[str, Any]]:
    """
    Late outs from Sportsbet Win/Place markets (isOut).
    Limited to max_events open AU/NZ TB races to keep load reasonable.
    """
    events = list_au_tb_events(meeting_date, ttl_seconds=ttl_seconds)
    # Prefer not-yet-resulted meetings; stable order by start time.
    openish = [e for e in events if not e.get("resulted")]
    openish.sort(key=lambda e: (e.get("start_time") is None, e.get("start_time") or 0))
    out: list[dict[str, Any]] = []
    for e in openish[: max(0, int(max_events))]:
        eid = e.get("event_id")
        if eid is None:
            continue
        odds = odds_by_horse_for_event(int(eid), ttl_seconds=min(ttl_seconds, 90))
        for row in odds.values():
            if not row.get("scratched"):
                continue
            out.append(
                {
                    "venue": e.get("venue") or "",
                    "race_no": e.get("race_no"),
                    "horse": row.get("name") or "",
                    "no": row.get("no"),
                    "source": "sportsbet",
                    "event_id": int(eid),
                }
            )
    return out
