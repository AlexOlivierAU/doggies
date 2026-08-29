"""Load and refresh thoroughbred meetings/fields without Streamlit.

Live fetches happen here (or in a worker that calls here). Callers must not
invoke this from a GUI paint path if `live=True`.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Optional

from db_cache import TTL_FIELDS, TTL_MEETINGS, get as db_get, set as db_set
from parse_racingaustralia import fetch_meetings_for_date, fetch_races_and_runners_for_meeting
from race_db import (
    _DEFAULT_DB,
    load_daily_fields,
    load_daily_meetings,
    merge_meeting_fields,
    persist_daily_fields,
    persist_daily_meetings,
)

log = logging.getLogger("race_day_rater.card")

MEETINGS_CODE = "Thoroughbred (All AU)"


@dataclass
class RefreshPayload:
    kind: str
    status: str  # success | partial | cached | failure
    message: str
    meetings: list = field(default_factory=list)
    fields_by_meeting: dict = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    from_cache: bool = False


def _nonempty_fields(mf: dict | None) -> bool:
    if not mf:
        return False
    return bool(mf.get("races") or mf.get("runners_by_race"))


def _as_tuple(mf: dict | None) -> tuple:
    mf = mf or {}
    return (mf.get("races") or [], mf.get("runners_by_race") or {}, mf.get("meta") or {})


def merge_fields_maps(base: dict, incoming: dict) -> dict:
    """Union of meeting cards. Empty incoming values never wipe a populated card."""
    out: dict[str, dict] = {}
    urls = set(base or {}) | set(incoming or {})
    for url in urls:
        old = (base or {}).get(url)
        new = (incoming or {}).get(url)
        if _nonempty_fields(old) and not _nonempty_fields(new):
            out[url] = old
            continue
        if _nonempty_fields(old) and _nonempty_fields(new):
            merged = merge_meeting_fields(_as_tuple(old), _as_tuple(new))
            out[url] = {"races": merged[0], "runners_by_race": merged[1], "meta": merged[2]}
            continue
        if _nonempty_fields(new):
            out[url] = new
        elif old:
            out[url] = old
    return out


def load_cached_card(
    chosen_date: date,
    db_path: Path = _DEFAULT_DB,
    meetings_code: str = MEETINGS_CODE,
) -> tuple[list, dict]:
    meetings = load_daily_meetings(chosen_date, meetings_code, db_path=db_path) or []
    if not meetings:
        meetings = load_daily_meetings(chosen_date, "thoroughbred", db_path=db_path) or []
    fields: dict[str, dict] = {}
    for m in meetings:
        url = getattr(m, "meeting_url", "") or ""
        if not url:
            continue
        data = load_daily_fields(chosen_date, url, db_path=db_path)
        if data is None:
            continue
        if len(data) == 2:
            races, runners, meta = data[0], data[1], {}
        else:
            races, runners, meta = data[0], data[1], data[2] if len(data) > 2 else {}
        fields[url] = {"races": races or [], "runners_by_race": runners or {}, "meta": meta or {}}
    return list(meetings), fields


def _meetings_cache_key(chosen_date: date) -> str:
    return f"meetings:tb:{chosen_date.isoformat()}:desktop"


def _fields_cache_key(meeting_url: str) -> str:
    digest = hashlib.sha1(meeting_url.encode()).hexdigest()[:16]
    return f"fields:tb:{digest}:desktop"


def fetch_tb_meetings(chosen_date: date, *, force: bool = False) -> list:
    key = _meetings_cache_key(chosen_date)
    if not force:
        cached = db_get(key, TTL_MEETINGS)
        if cached is not None:
            return list(cached)
    meetings = fetch_meetings_for_date(chosen_date, ttl_seconds=45 if force else 300)
    db_set(key, meetings)
    return list(meetings or [])


def fetch_tb_fields(meeting_url: str, *, force: bool = False) -> tuple:
    key = _fields_cache_key(meeting_url)
    if not force:
        cached = db_get(key, TTL_FIELDS)
        if cached is not None:
            return cached
    out = fetch_races_and_runners_for_meeting(meeting_url, ttl_seconds=45 if force else 300)
    db_set(key, out)
    return out


def refresh_card(
    chosen_date: date,
    *,
    previous_meetings: Optional[list] = None,
    previous_fields: Optional[dict] = None,
    db_path: Path = _DEFAULT_DB,
    live: bool = True,
    force: bool = False,
    meetings_code: str = MEETINGS_CODE,
) -> RefreshPayload:
    """Refresh TB card. Never returns empty maps when previous/cached data exists."""
    errors: list[str] = []
    cached_meetings, cached_fields = load_cached_card(chosen_date, db_path, meetings_code)
    meetings = list(previous_meetings or cached_meetings or [])
    fields = merge_fields_maps(cached_fields, previous_fields or {})
    live_ok = False
    live_partial = False
    used_cache = bool(meetings or fields)

    if live:
        try:
            fetched = fetch_tb_meetings(chosen_date, force=force)
            if fetched:
                meetings = fetched
                persist_daily_meetings(chosen_date, meetings_code, meetings, db_path=db_path)
                live_ok = True
            elif meetings:
                errors.append("Live meetings list was empty; kept cached meetings.")
                live_partial = True
            else:
                errors.append("No thoroughbred meetings from live source or cache.")
        except Exception as exc:
            log.warning("Meetings refresh failed: %s", exc)
            errors.append("Could not load meetings from Racing Australia.")
            if not meetings:
                return RefreshPayload(
                    kind="card",
                    status="failure",
                    message="Refresh failed",
                    meetings=list(previous_meetings or []),
                    fields_by_meeting=dict(previous_fields or {}),
                    errors=errors,
                    from_cache=used_cache,
                )
            live_partial = True

        incoming: dict[str, dict] = {}
        for m in meetings:
            url = getattr(m, "meeting_url", "") or ""
            if not url:
                continue
            live_tuple = None
            try:
                races, runners, meta = fetch_tb_fields(url, force=force)
                live_tuple = (races or [], runners or {}, meta or {})
            except Exception as exc:
                log.warning("Fields refresh failed for %s: %s", getattr(m, "venue", url), exc)
                errors.append(f"Could not refresh {getattr(m, 'venue', 'a meeting')}.")
                live_partial = True
            db_tuple = None
            db_data = load_daily_fields(chosen_date, url, db_path=db_path)
            if db_data is not None:
                if len(db_data) == 2:
                    db_tuple = (db_data[0], db_data[1], {})
                else:
                    db_tuple = (db_data[0], db_data[1], db_data[2] if len(db_data) > 2 else {})
            prev = fields.get(url)
            session_tuple = _as_tuple(prev) if _nonempty_fields(prev) else None
            merged: tuple = ([], {}, {})
            for part in (session_tuple, db_tuple, live_tuple):
                if part is None:
                    continue
                merged = merge_meeting_fields(merged, part) if (merged[0] or merged[1]) else part
            if merged[0] or merged[1]:
                incoming[url] = {"races": merged[0], "runners_by_race": merged[1], "meta": merged[2]}
                persist_daily_fields(chosen_date, url, merged, db_path=db_path)
                if live_tuple is not None:
                    live_ok = True
            elif _nonempty_fields(prev):
                incoming[url] = prev
        fields = merge_fields_maps(fields, incoming)

    if not meetings and not fields:
        return RefreshPayload(
            kind="card",
            status="failure" if live else "cached",
            message="No cached race data. Connect to the internet and refresh.",
            errors=errors,
            from_cache=False,
        )

    if live and live_ok and not live_partial and not errors:
        status, message = "success", "Last refresh successful"
    elif live and (live_ok or live_partial or used_cache):
        status, message = ("partial" if errors else "cached"), (
            "Partial source failure" if errors else "Offline/cached data"
        )
    else:
        status, message = "cached", "Offline/cached data"

    return RefreshPayload(
        kind="card",
        status=status,
        message=message,
        meetings=meetings,
        fields_by_meeting=fields,
        errors=errors,
        from_cache=not live or status == "cached",
    )


def make_odds_lookup(event_index: dict, by_event: dict) -> Any:
    from odds_sportsbet import lookup_event_id, norm_horse_name

    def lookup(venue: str, race_no, horse: str):
        if not horse or race_no is None or not event_index:
            return None
        try:
            rn = int(race_no)
        except (TypeError, ValueError):
            return None
        eid = lookup_event_id(event_index, str(venue or ""), rn)
        if eid is None:
            return None
        table = by_event.get(int(eid)) or {}
        return table.get(norm_horse_name(horse))

    return lookup


def fetch_odds_bundle(chosen_date: date, views: list, *, budget: int = 24) -> tuple[dict, dict, list[str]]:
    """Fetch Sportsbet odds for a limited set of views. Returns (index, by_event, errors)."""
    from odds_sportsbet import build_event_index, lookup_event_id, odds_by_horse_for_event

    errors: list[str] = []
    try:
        index = build_event_index(chosen_date, ttl_seconds=90)
    except Exception as exc:
        log.warning("Odds index failed: %s", exc)
        return {}, {}, ["Odds feed unavailable."]
    by_event: dict[int, dict] = {}
    seen = 0
    for view in views or []:
        if seen >= budget:
            break
        venue = getattr(view, "venue_raw", None) or getattr(view, "venue", "")
        race_no = getattr(view, "race_no", None)
        try:
            rn = int(race_no)
        except (TypeError, ValueError):
            continue
        eid = lookup_event_id(index, str(venue or ""), rn)
        if eid is None or int(eid) in by_event:
            continue
        try:
            by_event[int(eid)] = odds_by_horse_for_event(int(eid), ttl_seconds=60) or {}
            seen += 1
        except Exception as exc:
            log.warning("Odds event %s failed: %s", eid, exc)
            errors.append("Some odds could not be updated.")
    return index, by_event, errors
