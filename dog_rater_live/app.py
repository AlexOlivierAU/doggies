from __future__ import annotations

import copy
import re
from dataclasses import asdict
from datetime import date, datetime, time, timedelta
import hashlib
import html
from urllib.parse import quote
from zoneinfo import ZoneInfo

import streamlit as st

from fetch import FetchError
from parse_thedogs import (
    ParseError as DogsParseError,
    countdown_to_meeting,
    fetch_meetings_for_date as fetch_dog_meetings_for_date,
    fetch_races_for_meeting as fetch_dog_races_for_meeting,
    fetch_runners_for_race as fetch_dog_runners_for_race,
    next_upcoming_meeting,
    try_fetch_meetings_fallback as try_fetch_dog_meetings_fallback,
)
from parse_harness import ParseError as HarnessParseError
from parse_harness import fetch_meetings_for_date as fetch_harness_meetings_for_date
from parse_harness import fetch_races_and_runners_for_meeting as fetch_harness_races_and_runners
from parse_racingaustralia import ParseError as RacingAUSParseError
from parse_racingaustralia import enrich_runners_with_silks
from parse_racingaustralia import fetch_meetings_for_date as fetch_tb_meetings_for_date
from parse_racingaustralia import fetch_races_and_runners_for_meeting as fetch_tb_races_and_runners
from parse_racingaustralia import parse_race_class_label
from parse_racingaustralia import runner_class_arrow, runner_last_class
from parse_racingaustralia import runners_missing_last_class as tb_runners_missing_last_class
from parse_racingaustralia import runners_missing_silks as tb_runners_missing_silks
from parse_hrnz_nz import (
    ParseError as HrnzNzParseError,
    fetch_meetings_for_date as fetch_nz_harness_meetings_for_date,
    fetch_races_and_runners_for_meeting as fetch_nz_harness_races_and_runners,
)
from parse_nzracing import (
    ParseError as NzRacingParseError,
    fetch_meetings_for_date as fetch_nz_tb_meetings_for_date,
    fetch_races_and_runners_for_meeting as fetch_nz_tb_races_and_runners,
)
from parse_grnz import (
    fallback_hatrick_straight_meeting,
    fetch_meetings_for_date as fetch_grnz_meetings_for_date,
    fetch_races_and_runners_for_meeting as fetch_grnz_races_and_runners_for_meeting,
    fetch_races_for_meeting as fetch_grnz_races_for_meeting,
)
from parse_skyracing_schedule import fetch_sky_schedule
from models import Meeting
from scoring import normalize_weights, rank_runners, suggest_auto_weights
from history import history_bullets_for_runner, racingnsw_horse_history_bullets
from weather import venue_weather, venue_weather_for_race
from journal import load_picks as journal_load_picks, make_pick_entry, upsert_pick
from review import fetch_results_for_meeting
from backtest_compression import run_backtest, format_report
from odds_sportsbet import (
    build_event_index,
    collect_sportsbet_scratchings,
    format_odds_suffix,
    lookup_event_id,
    norm_horse_name as _odds_norm_horse,
    odds_by_horse_for_event,
)
from db_cache import get as db_get, set as db_set, TTL_MEETINGS, TTL_FIELDS, TTL_SKY
from race_db import (
    backfill_jockey_rides,
    db_status,
    jockey_stats,
    load_daily_fields as db_load_daily_fields,
    load_picks as db_load_picks,
    load_results as db_load_results,
    merge_meeting_fields as db_merge_meeting_fields,
    persist_daily_fields as db_persist_daily_fields,
    persist_daily_meetings as db_persist_daily_meetings,
    persist_results as db_persist_results,
    save_pick as db_save_pick,
    update_race_runners_in_db as db_update_race_runners,
)


def load_picks(d: date) -> list:
    """Merge journal (JSON) and race_db (SQL) picks; DB wins on same (meeting_url, race_no)."""
    j = journal_load_picks(d)
    db = db_load_picks(d)
    by_key: dict[tuple[str, int], dict] = {}
    for p in j:
        try:
            by_key[(p.get("meeting_url", ""), int(p.get("race_no") or 0))] = p
        except Exception:
            continue
    for p in db:
        try:
            by_key[(p.get("meeting_url", ""), int(p.get("race_no") or 0))] = p
        except Exception:
            continue
    return list(by_key.values())


def autosave_roster_picks(chosen_date: date, rows: list) -> int:
    """
    Persist roster best picks (and backup/roughie + scores) for Daily review / tracking.
    Upserts into race_db; skips rows without a best pick / meeting / race_no.
    """
    saved = 0
    for rr in rows or []:
        pick = (rr.get("best_pick") or "").strip()
        meeting_url = str(rr.get("meeting_link") or "").strip()
        race_no = rr.get("race_no")
        if not pick or not meeting_url or race_no is None:
            continue
        try:
            race_no_int = int(race_no)
        except (TypeError, ValueError):
            continue
        row_code = str(rr.get("_code") or "thoroughbred")
        backup = (rr.get("if_scratched") or "").strip()
        roughie = (rr.get("roughie") or "").strip()
        just_place = (rr.get("just_place") or "").strip()
        pick_no = rr.get("best_pick_no")
        pick_draw = None
        try:
            if pick_no not in (None, ""):
                pick_draw = int(str(pick_no).strip())
        except (TypeError, ValueError):
            pick_draw = None

        def _score(key: str) -> float | None:
            v = rr.get(key)
            if v is None or v == "":
                return None
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        best_score = _score("best_pick_score")
        backup_score = _score("if_scratched_score")
        roughie_score = _score("roughie_score")
        just_place_score = _score("just_place_score")
        field_size = rr.get("field_size")
        try:
            field_size_int = int(field_size) if field_size not in (None, "") else None
        except (TypeError, ValueError):
            field_size_int = None
        status = str(rr.get("status") or "")
        try:
            entry = make_pick_entry(
                meeting_date=chosen_date,
                code=row_code,
                venue=str(rr.get("venue") or ""),
                meeting_url=meeting_url,
                race_no=race_no_int,
                race_name=str(rr.get("race") or f"R{race_no_int}"),
                race_url=str(rr.get("race_link") or ""),
                pick_name=pick,
                pick_draw=pick_draw,
                pick_score=float(best_score) if best_score is not None else 0.0,
                key_factors=str(rr.get("why") or ""),
                why_bullets=list(rr.get("_best_pick_why") or [])[:8],
                history_bullets=[],
                weights={"source": "roster_autosave"},
                conditions={
                    "backup": backup,
                    "roughie": roughie,
                    "just_place": just_place,
                    "backup_score": backup_score,
                    "roughie_score": roughie_score,
                    "just_place_score": just_place_score,
                    "field_size": field_size_int,
                    "status": status,
                },
            )
            db_save_pick(
                chosen_date,
                entry.meeting_url,
                entry.code,
                entry.race_no,
                entry.venue,
                entry.race_name or f"R{entry.race_no}",
                entry.pick_name,
                backup=backup,
                pick_data=asdict(entry),
                roughie=roughie,
                best_score=best_score,
                backup_score=backup_score,
                roughie_score=roughie_score,
                field_size=field_size_int,
                status=status,
                just_place=just_place,
                just_place_score=just_place_score,
            )
            saved += 1
        except Exception:
            continue
    return saved


def refresh_one_race(
    chosen_date: date,
    meeting_url: str,
    row_code: str,
    race_no: int,
    race_link: str,
    meetings: list,
    fields_by_meeting: dict,
) -> tuple[bool, str]:
    """
    Re-fetch one race's data, update DB and fields_by_meeting. Returns (success, message).
    For AU greyhound: fetch runners for that race only. For TB/harness/NZ greyhound: re-fetch full meeting.
    """
    meeting_url = str(meeting_url or "").strip()
    if not meeting_url or race_no is None:
        return False, "Missing meeting or race number."
    m = next((x for x in meetings if getattr(x, "meeting_url", "") == meeting_url), None)
    meeting_date = chosen_date
    if m is not None:
        meeting_date = getattr(m, "meeting_date", chosen_date) or chosen_date
    try:
        def _current_meeting_data():
            cur = fields_by_meeting.get(meeting_url)
            if cur and (cur.get("races") or cur.get("runners_by_race")):
                return (cur.get("races"), cur.get("runners_by_race"), cur.get("meta") or {})
            if chosen_date:
                loaded = db_load_daily_fields(chosen_date, meeting_url)
                if loaded:
                    return (loaded[0], loaded[1], loaded[2] if len(loaded) > 2 else {})
            return None

        if row_code == "greyhound" and ("grnz.co.nz" in meeting_url or "grnz" in (race_link or "")):
            races, runners_by_race = fetch_grnz_races_and_runners_for_meeting(meeting_url, meeting_date, ttl_seconds=60)
            new_data = (races, runners_by_race, {})
            data = db_merge_meeting_fields(_current_meeting_data(), new_data)
            db_persist_daily_fields(chosen_date, meeting_url, data)
            fields_by_meeting[meeting_url] = {"races": data[0], "runners_by_race": data[1], "meta": data[2]}
            return True, "Meeting fields refreshed."
        elif row_code == "greyhound" and race_link:
            new_runners = fetch_dog_runners_for_race(race_link, ttl_seconds=0)
            ok = db_update_race_runners(chosen_date, meeting_url, race_no, new_runners)
            if not ok:
                return False, "Could not update DB (no stored fields for this meeting)."
            data = db_load_daily_fields(chosen_date, meeting_url)
            if data is None:
                return False, "DB update succeeded but reload failed."
            if len(data) == 2:
                races, runners_by_race = data[0], data[1]
                meta = {}
            else:
                races, runners_by_race = data[0], data[1]
                meta = data[2] if len(data) > 2 else {}
            fields_by_meeting[meeting_url] = {"races": races, "runners_by_race": runners_by_race, "meta": meta}
            return True, "Race field updated."
        elif row_code == "thoroughbred":
            if m and (getattr(m, "extra", {}) or {}).get("country") == "NZ":
                races, runners_by_race, meta = fetch_nz_tb_races_and_runners(meeting_url, meeting_date)
            else:
                races, runners_by_race, meta = fetch_tb_races_and_runners(meeting_url)
            meta = meta if isinstance(meta, dict) else {}
            new_data = (races, runners_by_race, meta)
            data = db_merge_meeting_fields(_current_meeting_data(), new_data)
            db_persist_daily_fields(chosen_date, meeting_url, data)
            fields_by_meeting[meeting_url] = {"races": data[0], "runners_by_race": data[1], "meta": data[2]}
            return True, "Meeting fields refreshed."
        elif row_code == "harness":
            if m and (getattr(m, "extra", {}) or {}).get("country") == "NZ":
                races, runners_by_race = fetch_nz_harness_races_and_runners(meeting_url, meeting_date)
            else:
                races, runners_by_race = fetch_harness_races_and_runners(meeting_url, meeting_date)
            new_data = (races, runners_by_race, {})
            data = db_merge_meeting_fields(_current_meeting_data(), new_data)
            db_persist_daily_fields(chosen_date, meeting_url, data)
            fields_by_meeting[meeting_url] = {"races": data[0], "runners_by_race": data[1], "meta": data[2]}
            return True, "Meeting fields refreshed."
        elif row_code == "greyhound":
            races, runners_by_race = fetch_grnz_races_and_runners_for_meeting(meeting_url, meeting_date, ttl_seconds=60)
            new_data = (races, runners_by_race, {})
            data = db_merge_meeting_fields(_current_meeting_data(), new_data)
            db_persist_daily_fields(chosen_date, meeting_url, data)
            fields_by_meeting[meeting_url] = {"races": data[0], "runners_by_race": data[1], "meta": data[2]}
            return True, "Meeting fields refreshed."
    except Exception as e:
        return False, str(e)
    return False, "Unsupported code."

try:
    from st_aggrid import AgGrid, JsCode
    from st_aggrid.grid_options_builder import GridOptionsBuilder
    _AGGRID_AVAILABLE = True
except ImportError:
    _AGGRID_AVAILABLE = False
    JsCode = None  # type: ignore

st.set_page_config(page_title="dog_rater_live", layout="wide")


@st.cache_data(show_spinner=False)
def cached_next_greyhound_pick(meeting_url: str, meeting_date: date, venue: str):
    """
    Best-effort: find the next upcoming race within a meeting and compute a top pick + why.
    Cached because this does network I/O.
    """
    now = datetime.now().astimezone()
    try:
        races = fetch_dog_races_for_meeting(meeting_url, ttl_seconds=60)
    except Exception:
        return None
    if not races:
        return None

    best = None  # (dt, race)
    for r in races:
        stt = getattr(r, "start_time_local", None)
        if not isinstance(stt, time):
            continue
        dt = datetime.combine(meeting_date, stt, tzinfo=now.tzinfo)
        if dt >= now and (best is None or dt < best[0]):
            best = (dt, r)
    if best is None:
        return None

    dt, race = best
    try:
        runners = fetch_dog_runners_for_race(race.race_url, ttl_seconds=60)
    except Exception:
        return None
    if not runners:
        return None

    bw, fw, ew, _rationale = suggest_auto_weights(runners, weather=None, track_condition=None)
    ranked = rank_runners(
        runners,
        box_weight=bw,
        form_weight=fw,
        early_weight=ew,
        weather=None,
        track_condition=None,
        explain_mode="short",
    )
    if not ranked:
        return None
    top = ranked[0]
    return {
        "venue": venue,
        "race_no": race.race_no,
        "race_time": race.start_time_local.strftime("%H:%M") if isinstance(race.start_time_local, time) else "",
        "race_url": race.race_url,
        "pick_name": top.name,
        "pick_score": float(top.score),
        "why_bullets": list(top.why_bullets)[:6],
    }


@st.cache_data(show_spinner=False)
def cached_dog_meetings(d: date) -> list:
    key = f"meetings:dog:{d.isoformat()}"
    out = db_get(key, TTL_MEETINGS)
    if out is not None:
        return out
    out = fetch_dog_meetings_for_date(d, ttl_seconds=30 * 60)
    db_set(key, out)
    return out


@st.cache_data(show_spinner=False)
def cached_dog_races(meeting_url: str) -> list:
    key = f"races:dog:{hashlib.sha1(meeting_url.encode()).hexdigest()[:16]}"
    out = db_get(key, TTL_FIELDS)
    if out is not None:
        return out
    out = fetch_dog_races_for_meeting(meeting_url, ttl_seconds=30 * 60)
    db_set(key, out)
    return out


def cached_dog_runners(race_url: str) -> list:
    """Fetch greyhound runners for a race. No Streamlit cache (return value not pickle-safe); fetch layer still caches HTML."""
    return fetch_dog_runners_for_race(race_url, ttl_seconds=10 * 60)


@st.cache_data(show_spinner=False)
def cached_tb_meetings(d: date, refresh_nonce: int = 0) -> list:
    key = f"meetings:tb:{d.isoformat()}:{refresh_nonce}"
    out = db_get(key, TTL_MEETINGS)
    if out is not None:
        return out
    out = fetch_tb_meetings_for_date(d)
    db_set(key, out)
    return out


@st.cache_data(show_spinner=False)
def cached_tb_fields(meeting_url: str, refresh_nonce: int = 0) -> tuple[list, dict, dict]:
    key = f"fields:tb:{hashlib.sha1(meeting_url.encode()).hexdigest()[:16]}:{refresh_nonce}"
    out = db_get(key, TTL_FIELDS)
    if out is not None:
        return out
    out = fetch_tb_races_and_runners(meeting_url)
    db_set(key, out)
    return out


@st.cache_data(show_spinner=False)
def cached_harness_meetings(d: date) -> list:
    key = f"meetings:harness:{d.isoformat()}"
    out = db_get(key, TTL_MEETINGS)
    if out is not None:
        return out
    out = fetch_harness_meetings_for_date(d)
    db_set(key, out)
    return out


@st.cache_data(show_spinner=False)
def cached_harness_fields(meeting_url: str, meeting_date: date) -> tuple[list, dict]:
    key = f"fields:harness:{hashlib.sha1(meeting_url.encode()).hexdigest()[:16]}:{meeting_date.isoformat()}"
    out = db_get(key, TTL_FIELDS)
    if out is not None:
        return out
    out = fetch_harness_races_and_runners(meeting_url, meeting_date)
    db_set(key, out)
    return out


# --- NZ: Harness NZ (HRNZ) implemented; greyhound via GRNZ (Hatrick Straight, etc.) ---
@st.cache_data(show_spinner=False)
def cached_nz_dog_meetings(d: date) -> list:
    key = f"meetings:nz_dog:{d.isoformat()}"
    out = db_get(key, TTL_MEETINGS)
    if out is not None:
        return out
    try:
        out = fetch_grnz_meetings_for_date(d, ttl_seconds=30 * 60)
        db_set(key, out)
        return out
    except Exception:
        return []


@st.cache_data(show_spinner=False)
def cached_nz_dog_fields(meeting_url: str, meeting_date: date) -> tuple[list, dict]:
    """NZ greyhound: races + runners from GRNZ (or placeholder + empty on failure)."""
    key = f"fields:nz_dog:{hashlib.sha1(meeting_url.encode()).hexdigest()[:16]}:{meeting_date.isoformat()}"
    out = db_get(key, TTL_FIELDS)
    if out is not None:
        return out
    try:
        out = fetch_grnz_races_and_runners_for_meeting(meeting_url, meeting_date, ttl_seconds=30 * 60)
        db_set(key, out)
        return out
    except Exception:
        races = fetch_grnz_races_for_meeting(meeting_url, meeting_date)
        runners_by_race = {getattr(r, "race_no", i): [] for i, r in enumerate(races or [], 1)}
        out = (races or [], runners_by_race)
        db_set(key, out)
        return out


@st.cache_data(show_spinner=False)
def cached_nz_harness_meetings(d: date) -> list:
    key = f"meetings:nz_harness:{d.isoformat()}"
    out = db_get(key, TTL_MEETINGS)
    if out is not None:
        return out
    try:
        out = fetch_nz_harness_meetings_for_date(d, ttl_seconds=30 * 60)
        db_set(key, out)
        return out
    except Exception:
        return []


@st.cache_data(show_spinner=False)
def cached_nz_harness_fields(meeting_url: str, meeting_date: date) -> tuple[list, dict]:
    key = f"fields:nz_harness:{hashlib.sha1(meeting_url.encode()).hexdigest()[:16]}:{meeting_date.isoformat()}"
    out = db_get(key, TTL_FIELDS)
    if out is not None:
        return out
    out = fetch_nz_harness_races_and_runners(meeting_url, meeting_date)
    db_set(key, out)
    return out


@st.cache_data(show_spinner=False)
def cached_nz_tb_meetings(d: date, refresh_nonce: int = 0) -> list:
    key = f"meetings:nz_tb:{d.isoformat()}:{refresh_nonce}"
    out = db_get(key, TTL_MEETINGS)
    if out is not None:
        return out
    out = fetch_nz_tb_meetings_for_date(d, ttl_seconds=30 * 60)
    db_set(key, out)
    return out


@st.cache_data(show_spinner=False)
def cached_nz_tb_fields(meeting_url: str, meeting_date: date) -> tuple[list, dict, dict]:
    """NZ thoroughbred: races + runners from nzracing.co.nz (races/runners may be empty if not yet parsed)."""
    key = f"fields:nz_tb:{hashlib.sha1(meeting_url.encode()).hexdigest()[:16]}:{meeting_date.isoformat()}"
    out = db_get(key, TTL_FIELDS)
    if out is not None:
        return out
    races, runners_by_race, _ = fetch_nz_tb_races_and_runners(meeting_url, meeting_date)
    out = (races, runners_by_race, {})
    db_set(key, out)
    return out


@st.cache_data(show_spinner=False)
def cached_sky_schedule(d: date) -> list[dict]:
    """Sky Racing 1/2 schedule for overlay (schedule.skyracing.com.au). Best-effort."""
    key = f"sky:{d.isoformat()}"
    out = db_get(key, TTL_SKY)
    if out is not None:
        return out
    try:
        out = fetch_sky_schedule(d, ttl_seconds=30 * 60)
        db_set(key, out)
        return out
    except Exception:
        return []


def _meetings_with_country(meetings: list, country: str, chosen_date: date) -> list[Meeting]:
    """Normalise meetings to have extra['country'] = country (Meeting is frozen -> new instances)."""
    out: list[Meeting] = []
    for m in meetings:
        c = getattr(m, "code", None) or ""
        if c not in ("greyhound", "thoroughbred", "harness"):
            continue
        extra = dict(getattr(m, "extra", None) or {}, **{"country": country})
        out.append(
            Meeting(
                code=c,
                source=getattr(m, "source", ""),
                venue=getattr(m, "venue", ""),
                meeting_date=getattr(m, "meeting_date", chosen_date),
                first_race_time_local=getattr(m, "first_race_time_local", None),
                num_races=getattr(m, "num_races", None),
                meeting_url=getattr(m, "meeting_url", ""),
                status=getattr(m, "status", "unknown"),
                extra=extra,
            )
        )
    return out


def _tz_for_greyhound_meeting(m) -> ZoneInfo | None:
    """
    Map greyhound meeting -> track local timezone (NZ = Auckland; AU = infer from venue).
    Times from thedogs/GRNZ are in track local time.
    """
    try:
        if getattr(m, "code", "") != "greyhound":
            return None
        extra = getattr(m, "extra", {}) or {}
        if extra.get("country") == "NZ" or getattr(m, "source", "") == "grnz_nz":
            return ZoneInfo("Pacific/Auckland")
        venue = (getattr(m, "venue", "") or "").upper()
        if "ANGLE PARK" in venue or "ADELAIDE" in venue:
            return ZoneInfo("Australia/Adelaide")
        if "LAUNCESTON" in venue or "HOBART" in venue or "TASMAN" in venue:
            return ZoneInfo("Australia/Sydney")
        if "CANNINGTON" in venue or "MANDURAH" in venue or "WESTERN" in venue:
            return ZoneInfo("Australia/Perth")
        if "ALBION" in venue or "BRISBANE" in venue or "GOLD COAST" in venue or "IPA" in venue:
            return ZoneInfo("Australia/Brisbane")
        if "DARWIN" in venue or "KATHERINE" in venue:
            return ZoneInfo("Australia/Darwin")
        return ZoneInfo("Australia/Sydney")
    except Exception:
        return None
    return None


def get_meetings_for_code(code_label: str, chosen_date: date, refresh_nonce: int = 0) -> list:
    """
    Return meetings for the selected UI code. For "All (AU)" or "All (AU+NZ)" concatenate
    sources and ensure each meeting has m.code and m.extra["country"] set.
    """
    if code_label == "Greyhounds":
        meetings = cached_dog_meetings(chosen_date)
        if not meetings:
            meetings = try_fetch_dog_meetings_fallback(chosen_date)
        return meetings or []
    if code_label == "Thoroughbred (All AU)":
        return cached_tb_meetings(chosen_date, refresh_nonce)
    if code_label == "Thoroughbred (AU + NZ)":
        au_tb = cached_tb_meetings(chosen_date, refresh_nonce)
        nz_tb = cached_nz_tb_meetings(chosen_date, refresh_nonce)
        au = _meetings_with_country(au_tb, "AU", chosen_date)
        nz = _meetings_with_country(nz_tb, "NZ", chosen_date)
        return au + nz
    if code_label == "Harness (NSW)":
        return cached_harness_meetings(chosen_date)
    if code_label == "Greyhounds (NZ)":
        nz_dog = cached_nz_dog_meetings(chosen_date)
        if not nz_dog:
            nz_dog = [fallback_hatrick_straight_meeting(chosen_date)]
        return _meetings_with_country(nz_dog, "NZ", chosen_date)
    if code_label == "Harness (NZ)":
        return _meetings_with_country(cached_nz_harness_meetings(chosen_date), "NZ", chosen_date)
    if code_label == "Thoroughbred (NZ)":
        return _meetings_with_country(cached_nz_tb_meetings(chosen_date, refresh_nonce), "NZ", chosen_date)
    if code_label == "All (AU)":
        dog = cached_dog_meetings(chosen_date) or try_fetch_dog_meetings_fallback(chosen_date) or []
        tb = cached_tb_meetings(chosen_date, refresh_nonce)
        harness = cached_harness_meetings(chosen_date)
        return _meetings_with_country(dog + tb + harness, "AU", chosen_date)
    if code_label == "All (AU+NZ)":
        au_dog = cached_dog_meetings(chosen_date) or try_fetch_dog_meetings_fallback(chosen_date) or []
        au_tb = cached_tb_meetings(chosen_date, refresh_nonce)
        au_harness = cached_harness_meetings(chosen_date)
        nz_dog = cached_nz_dog_meetings(chosen_date)
        if not nz_dog:
            nz_dog = [fallback_hatrick_straight_meeting(chosen_date)]
        nz_harness = cached_nz_harness_meetings(chosen_date)
        nz_tb = cached_nz_tb_meetings(chosen_date, refresh_nonce)
        au = _meetings_with_country(au_dog + au_tb + au_harness, "AU", chosen_date)
        nz = _meetings_with_country(nz_dog + nz_harness + nz_tb, "NZ", chosen_date)
        return au + nz
    return []


@st.cache_data(show_spinner=False)
def cached_tb_history(profile_url: str) -> list[str]:
    # Racing NSW-only enrichment; Racing Australia horse links won't work here (best-effort).
    if not profile_url or "racingnsw" not in (profile_url or "").lower():
        return []
    return racingnsw_horse_history_bullets(profile_url)


@st.cache_data(show_spinner=False, ttl=90)
def cached_odds_event_index(d: date) -> dict:
    """Sportsbet (venue_key, race_no) -> event_id for the day."""
    try:
        return build_event_index(d, ttl_seconds=90)
    except Exception:
        return {}


@st.cache_data(show_spinner=False, ttl=60)
def cached_race_odds(event_id: int) -> dict:
    """horse_norm -> odds dict for one Sportsbet event."""
    try:
        return odds_by_horse_for_event(int(event_id), ttl_seconds=60)
    except Exception:
        return {}


@st.cache_data(show_spinner=False, ttl=120)
def cached_sportsbet_scratchings(d: date) -> list[dict]:
    try:
        return collect_sportsbet_scratchings(d, max_events=35, ttl_seconds=120)
    except Exception:
        return []


@st.cache_data(show_spinner=False)
def cached_venue_weather(venue: str):
    return venue_weather(venue)

@st.cache_data(show_spinner=False)
def cached_race_weather(venue: str, meeting_date: date, start_time_local):
    return venue_weather_for_race(venue, meeting_date, start_time_local)


def render_roster_content(*, chosen_date: date, code_label: str, meetings: list, fields_by_meeting: dict, open_nonce: int = 0) -> None:
    """Render the race roster (AG Grid or table). Can be used inline or inside the roster dialog."""
    # Widen when in dialog (Streamlit dialogs are otherwise fairly narrow).
    # Best-effort: if Streamlit changes DOM/testids, this may stop working harmlessly.
    st.markdown(
        """
<style>
  /* Make the roster dialog much wider (near full-screen). */
  /* Streamlit dialogs are BaseWeb modals; target a few likely wrappers. */
  div[data-testid="stDialog"] [role="dialog"],
  div[data-testid="stDialog"] div[role="dialog"],
  div[data-baseweb="modal"] > div,
  div[data-baseweb="modal"] div[role="dialog"] {
    width: calc(100vw - 4rem) !important;
    max-width: 2400px !important;
    min-width: 1200px !important;
  }

  /* Ensure the modal container itself can stretch. */
  div[data-testid="stDialog"],
  div[data-baseweb="modal"] {
    width: calc(100vw - 2rem) !important;
    max-width: 2400px !important;
  }
</style>
""",
        unsafe_allow_html=True,
    )

    st.write(f"**Date:** {chosen_date.isoformat()}")

    # Single-code label -> code; for "All (AU)" / "All (AU+NZ)" we use m.code per meeting inside the loop.
    code = (
        "greyhound"
        if code_label in ("Greyhounds", "Greyhounds (NZ)")
        else "thoroughbred"
        if code_label.startswith("Thoroughbred")
        else "harness"
        if code_label in ("Harness (NSW)", "Harness (NZ)")
        else ""
    )
    if not code and code_label in ("All (AU)", "All (AU+NZ)"):
        code = "greyhound"  # fallback for per_race default; each row uses m.code
    tz_name = st.session_state.get("tz_name") or "Australia/Sydney"
    tz = None
    if tz_name and tz_name != "Local (server)":
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = None
    app_tz = tz
    # Keep `now` in the app timezone (do not .astimezone() to the server local zone).
    now = datetime.now(tz) if tz is not None else datetime.now().astimezone()
    now_str = now.strftime("%H:%M")

    # Default per-code race duration (used only when row_code not set); per-row we use per_race_m.
    per_race = timedelta(minutes=25 if code == "greyhound" else 35 if code == "thoroughbred" else 30)

    # Streamlit remembers widget state; force defaults each time the roster is opened.
    filters_key = (code_label, chosen_date.isoformat(), int(open_nonce or 0))
    if st.session_state.get("roster_filters_key") != filters_key:
        st.session_state.roster_filters_key = filters_key
        st.session_state.roster_show_finished = True  # full card; keep finished races from DB/snapshot
        st.session_state.roster_only_next_per_venue = False
        st.session_state.roster_type_filter = "all"
        st.session_state.roster_show_t = True
        st.session_state.roster_show_h = True
        st.session_state.roster_show_d = True
        st.session_state.roster_show_best_pick = True
        st.session_state.roster_pick_limit = 0  # 0 = no limit (picks for all races)
        st.session_state.roster_trim_finished_minutes = 20  # retention hint for merge/reload
        st.session_state.roster_overlay_sky = True

    # Use defaults (no UI for these): show full card for the day.
    show_finished = st.session_state.get("roster_show_finished", True)
    trim_finished_minutes = int(st.session_state.get("roster_trim_finished_minutes", 20) or 20)
    type_filter = st.session_state.get("roster_type_filter", "all")
    only_next_per_venue = st.session_state.get("roster_only_next_per_venue", False)
    # Show/hide T, H, D when roster is mixed (All AU or All AU+NZ)
    if code_label in ("All (AU)", "All (AU+NZ)"):
        bt, bh, bd, bfin = st.columns([1, 1, 1, 2])
        with bt:
            show_t = st.checkbox("T", value=st.session_state.get("roster_show_t", True), key="roster_show_t", help="Show Thoroughbred")
        with bh:
            show_h = st.checkbox("H", value=st.session_state.get("roster_show_h", True), key="roster_show_h", help="Show Harness")
        with bd:
            show_d = st.checkbox("D", value=st.session_state.get("roster_show_d", True), key="roster_show_d", help="Show Greyhound (Dogs)")
        with bfin:
            st.caption(
                "Full card for the day (including earlier races) from stored/DB data. "
                "Refresh keeps races the live source has dropped."
            )
    else:
        show_t = show_h = show_d = True
    st.caption(
        f"**Current time (app):** **{now_str}** ({tz_name}). "
        "Grid: full card for the day (earlier races kept from DB/snapshot on reload). "
        "Table times from data source (often venue local). Field size = declared minus scratches we detect; "
        "use **Update race** in a row to refresh if it doesn’t match the official field."
    )

    rows = []
    next_race = None  # (dt, row)
    used_approx = False

    # Defaults: show best pick on; 0 = no limit (compute picks for all races in grid).
    show_best_pick = st.session_state.get("roster_show_best_pick", True)
    pick_limit = int(st.session_state.get("roster_pick_limit", 0))
    computed_picks = 0

    def _truncate(s: str, n: int = 90) -> str:
        s = (s or "").strip()
        if len(s) <= n:
            return s
        return s[: n - 1].rstrip() + "…"

    def _runner_number_for_name(runners: list, name: str) -> str:
        """
        Best-effort extraction of a runner number for display (e.g. "7. NOVALARGO").
        Matches what TV shows: program/saddle cloth number for thoroughbreds, not barrier.
        - Greyhound: uses Runner.draw (box number).
        - Thoroughbred: prefers 'No' / program number from Acceptances (what TV shows), else barrier.
        - Harness: leading number in the original name cell (stored in raw['cells'][1]).
        Returns "" if unknown/not applicable.
        """
        if not runners or not name:
            return ""
        r_obj = next((x for x in runners if getattr(x, "name", None) == name), None)
        if r_obj is None:
            return ""
        code = getattr(r_obj, "code", "") or ""
        draw = getattr(r_obj, "draw", None)

        if code == "thoroughbred":
            # TV shows program/saddle cloth number ("No"), not barrier. Prefer No when present.
            raw = getattr(r_obj, "raw", {}) or {}
            headers = raw.get("headers") or []
            cells = raw.get("cells") or []
            hn = [str(h).strip().lower() for h in headers]
            idx = None
            for i, h in enumerate(hn):
                if h in {"no.", "no", "number", "saddle", "program", "#", "num", "runner no", "runner no."}:
                    idx = i
                    break
            if idx is not None and 0 <= idx < len(cells):
                import re
                m = re.search(r"\b(\d{1,2})\b", str(cells[idx]))
                if m:
                    return m.group(1)
            if draw is not None:
                return str(draw)
            return ""

        # Greyhound (and any code with draw): use box/draw number when present
        if draw is not None:
            return str(draw)

        if code == "harness":
            raw = getattr(r_obj, "raw", {}) or {}
            cells = raw.get("cells") or []
            if len(cells) >= 2:
                import re

                m = re.match(r"^\s*(\d{1,2})\s+", str(cells[1]))
                if m:
                    return m.group(1)
            return ""

        return ""

    def _silk_url_for_name(runners: list, name: str) -> str:
        if not runners or not name:
            return ""
        r_obj = next((x for x in runners if getattr(x, "name", None) == name), None)
        if r_obj is None:
            return ""
        return str(getattr(r_obj, "silk_url", None) or "")

    def _class_arrow_for_name(runners: list, name: str, today_class: str) -> str:
        if not runners or not name:
            return ""
        r_obj = next((x for x in runners if getattr(x, "name", None) == name), None)
        if r_obj is None:
            return ""
        return runner_class_arrow(r_obj, today_class or "")

    def _barrier_for_name(runners: list, name: str) -> str:
        if not runners or not name:
            return ""
        r_obj = next((x for x in runners if getattr(x, "name", None) == name), None)
        if r_obj is None:
            return ""
        draw = getattr(r_obj, "draw", None)
        return str(draw) if draw is not None else ""

    def _with_class_arrow(disp: str, arrow: str) -> str:
        if not disp or not arrow:
            return disp
        return f"{disp} {arrow}"

    def _format_pick_cell(
        *,
        name: str,
        no: str,
        runners: list,
        today_class: str,
        prefix: str = "",
        odds_suffix: str = "",
    ) -> str:
        """e.g. '3. Horse (7) $4.8↓ ↑' — program no, name, barrier, odds/fluc, class arrow."""
        if not name:
            return ""
        core = f"{no}. {name}" if no else name
        bar = _barrier_for_name(runners, name)
        if bar:
            core = f"{core} ({bar})"
        if odds_suffix:
            core = f"{core}{odds_suffix}"
        if prefix:
            core = f"{prefix}{core}"
        return _with_class_arrow(core, _class_arrow_for_name(runners, name, today_class))

    # Sportsbet odds index for the day (best-effort; empty if feed unavailable).
    _want_odds = code_label.startswith("Thoroughbred") or code_label in ("All (AU)", "All (AU+NZ)")
    _odds_event_idx = cached_odds_event_index(chosen_date) if _want_odds else {}
    _odds_by_event: dict[int, dict] = {}
    _odds_fetch_budget = 35  # unique races per roster paint

    def _odds_row_for(venue: str, race_no, horse: str) -> dict | None:
        nonlocal _odds_fetch_budget
        if not horse or race_no is None or not _odds_event_idx:
            return None
        try:
            rn = int(race_no)
        except Exception:
            return None
        eid = lookup_event_id(_odds_event_idx, str(venue or ""), rn)
        if eid is None:
            return None
        if eid not in _odds_by_event:
            if _odds_fetch_budget <= 0:
                return None
            _odds_by_event[eid] = cached_race_odds(int(eid)) or {}
            _odds_fetch_budget -= 1
        return (_odds_by_event.get(eid) or {}).get(_odds_norm_horse(horse))

    def _odds_suffix_for(venue: str, race_no, horse: str) -> str:
        o = _odds_row_for(venue, race_no, horse)
        if not o or o.get("scratched"):
            return ""
        return format_odds_suffix(o.get("win"), o.get("fluc") or "")

    def _patch_detail_odds(detail_json: str, venue: str, race_no, horse: str) -> str:
        import json as _json

        if not detail_json:
            return detail_json
        o = _odds_row_for(venue, race_no, horse)
        if not o:
            return detail_json
        try:
            d = _json.loads(detail_json)
        except Exception:
            return detail_json
        if o.get("win") is not None:
            d["win_odds"] = o.get("win")
        if o.get("place") is not None:
            d["place_odds"] = o.get("place")
        d["fluc"] = o.get("fluc") or ""
        d["flucs"] = o.get("flucs") or []
        try:
            return _json.dumps(d, ensure_ascii=False)
        except Exception:
            return detail_json

    def _pick_detail_json(
        runners: list,
        *,
        role: str,
        name: str,
        no: str,
        silk: str,
        why_bullets: list | None,
        race_row: dict,
    ) -> str:
        """Compact JSON for right-click pick popup (read by AG Grid JS)."""
        import json

        r_obj = next((x for x in (runners or []) if getattr(x, "name", None) == name), None) if name else None
        detail = {
            "role": role or "",
            "name": name or "",
            "no": no or "",
            "silk": silk or "",
            "why": [str(b) for b in (why_bullets or [])[:8]],
            "venue": str(race_row.get("venue") or ""),
            "race": str(race_row.get("race") or ""),
            "time": str(race_row.get("time") or ""),
            "distance": str(race_row.get("race_length") or ""),
            "field_size": str(race_row.get("field_size") if race_row.get("field_size") is not None else ""),
            "class_label": str(race_row.get("class") or ""),
            "track": str(race_row.get("track") or ""),
            "last_class": "",
            "class_arrow": "",
            "jockey": "",
            "trainer": "",
            "barrier": "",
            "age": "",
            "sex": "",
            "weight": "",
            "benchmark": "",
            "last10": "",
            "profile_url": "",
        }
        if r_obj is not None:
            detail["jockey"] = str(getattr(r_obj, "jockey_or_driver", None) or "")
            detail["trainer"] = str(getattr(r_obj, "trainer", None) or "")
            draw = getattr(r_obj, "draw", None)
            detail["barrier"] = str(draw) if draw is not None else ""
            age = getattr(r_obj, "age", None)
            detail["age"] = str(age) if age is not None else ""
            detail["sex"] = str(getattr(r_obj, "sex", None) or "")
            wt = getattr(r_obj, "weight_kg", None)
            detail["weight"] = f"{wt}kg" if wt is not None else ""
            bm = getattr(r_obj, "benchmark", None)
            detail["benchmark"] = str(bm) if bm is not None else ""
            detail["last10"] = str(getattr(r_obj, "last10", None) or "")
            detail["profile_url"] = str(getattr(r_obj, "profile_url", None) or "")
            today_cls = detail["class_label"]
            detail["last_class"] = runner_last_class(r_obj)
            detail["class_arrow"] = runner_class_arrow(r_obj, today_cls)
        return json.dumps(detail, ensure_ascii=False)

    def _runners_objects_for_roster_row(r: dict) -> list:
        """Unique runner objects for a roster row (same source rules as field list)."""
        row_code = r.get("_code") or code
        race_link = str(r.get("race_link") or "")
        if row_code == "greyhound":
            if "grnz.co.nz" in race_link or r.get("_source") == "grnz_nz":
                mf = fields_by_meeting.get(str(r.get("meeting_link") or ""), {}) or {}
                runners_by = mf.get("runners_by_race") or {}
                rn = r.get("race_no")
                runners = runners_by.get(rn, []) if rn is not None else []
            else:
                runners = cached_dog_runners(race_link)
        else:
            mf = fields_by_meeting.get(str(r.get("meeting_link") or ""), {}) or {}
            runners_by = mf.get("runners_by_race") or {}
            rn = r.get("race_no")
            runners = runners_by.get(rn, []) if rn is not None else []
        seen_names: set[str] = set()
        unique: list = []
        for runner in runners or []:
            name = (getattr(runner, "name", "") or "").strip()
            if not name or name in seen_names:
                continue
            seen_names.add(name)
            unique.append(runner)
        return unique

    def _field_grid_json(r: dict) -> str:
        """Full field payload for Shift+click grid popup (read by AG Grid JS)."""
        import json

        runners = _runners_objects_for_roster_row(r)
        best = (r.get("best_pick") or "").strip()
        backup = (r.get("if_scratched") or "").strip()
        place = (r.get("just_place") or "").strip()
        roughie = (r.get("roughie") or "").strip()
        today_cls = str(r.get("class") or "")
        out_runners = []
        used_nums: set[str] = set()
        for i, runner in enumerate(runners):
            name = getattr(runner, "name", "") or ""
            raw_num = _runner_number_for_name(runners, name) or (
                str(getattr(runner, "draw", "")) if getattr(runner, "draw", None) is not None else str(i + 1)
            )
            num = raw_num if raw_num not in used_nums else str(i + 1)
            used_nums.add(num)
            mark = ""
            if name == best:
                mark = "Pick"
            elif name == place:
                mark = "Place"
            elif name == backup:
                mark = "Backup"
            elif name == roughie:
                mark = "Roughie"
            draw = getattr(runner, "draw", None)
            wt = getattr(runner, "weight_kg", None)
            bm = getattr(runner, "benchmark", None)
            last_cls = runner_last_class(runner)
            arrow = runner_class_arrow(runner, today_cls)
            out_runners.append(
                {
                    "no": num,
                    "name": name,
                    "silk": str(getattr(runner, "silk_url", None) or ""),
                    "scratched": bool(getattr(runner, "scratched", False)),
                    "barrier": str(draw) if draw is not None else "",
                    "jockey": str(getattr(runner, "jockey_or_driver", None) or ""),
                    "trainer": str(getattr(runner, "trainer", None) or ""),
                    "weight": f"{wt}kg" if wt is not None else "",
                    "benchmark": str(bm) if bm is not None else "",
                    "last10": str(getattr(runner, "last10", None) or ""),
                    "last_class": last_cls,
                    "class_arrow": arrow,
                    "mark": mark,
                }
            )
        return json.dumps(
            {
                "venue": str(r.get("venue") or ""),
                "race": str(r.get("race") or ""),
                "time": str(r.get("time") or ""),
                "distance": str(r.get("race_length") or ""),
                "class_label": today_cls,
                "track": str(r.get("track") or ""),
                "runners": out_runners,
            },
            ensure_ascii=False,
        )

    def _runners_for_roster_row(r: dict) -> list:
        """Return list of (display_no, name, scratched, silk_url) for the race in this roster row.
        Runners are deduplicated by name. Display numbers are forced unique to avoid
        duplicate program numbers (e.g. two 6s) from source data or merged lists.
        """
        unique_runners = _runners_objects_for_roster_row(r)
        out = []
        used_nums: set[str] = set()
        for i, runner in enumerate(unique_runners):
            name = getattr(runner, "name", "") or ""
            raw_num = _runner_number_for_name(unique_runners, name) or (
                str(getattr(runner, "draw", "")) if getattr(runner, "draw", None) is not None else str(i + 1)
            )
            # Ensure display number is unique: if already used, use 1-based position
            num = raw_num
            if num in used_nums:
                num = str(i + 1)
            used_nums.add(num)
            scratched = bool(getattr(runner, "scratched", False))
            silk = str(getattr(runner, "silk_url", None) or "")
            out.append((num, name, scratched, silk))
        return out

    # Keep quick actions visible near the top (dialogs can be tall; users may not scroll).
    if "roster_selected" not in st.session_state:
        st.session_state.roster_selected = None

    def _tz_for_tb_meeting(m) -> ZoneInfo | None:
        """
        Map meeting -> timezone for correct ordering (AU state or NZ).
        """
        try:
            if getattr(m, "code", "") != "thoroughbred":
                return None
            if (getattr(m, "extra", {}) or {}).get("country") == "NZ":
                return ZoneInfo("Pacific/Auckland")
            st_code = (getattr(m, "extra", {}) or {}).get("state") or ""
            st_code = str(st_code).upper().strip()
            if st_code in {"NSW", "VIC", "TAS", "ACT"}:
                return ZoneInfo("Australia/Sydney")
            if st_code == "QLD":
                return ZoneInfo("Australia/Brisbane")
            if st_code == "SA":
                return ZoneInfo("Australia/Adelaide")
            if st_code == "NT":
                return ZoneInfo("Australia/Darwin")
            if st_code == "WA":
                return ZoneInfo("Australia/Perth")
        except Exception:
            return None
        return None

    for m in meetings:
        # Skip barrier trials — they clutter the picks grid and often have no acceptances.
        _m_key = str((getattr(m, "extra", {}) or {}).get("key") or "")
        if ",Trial" in _m_key or _m_key.endswith("Trial"):
            continue
        mf = fields_by_meeting.get(getattr(m, "meeting_url", ""), {}) or {}
        races = mf.get("races") or []
        meeting_meta = mf.get("meta") or {}
        track_condition_raw = str(meeting_meta.get("track_condition") or "").strip()
        if track_condition_raw.upper() in {"N/A", "NA", "-", ""}:
            track_condition_disp = ""
        else:
            # Compact for grid: "Soft 5" -> "Soft5", "Heavy 10" -> "Heavy10"
            _tc_m = re.match(
                r"^(Firm|Good|Soft|Heavy|Synth(?:etic)?)\s*(\d+)?\b",
                track_condition_raw,
                re.IGNORECASE,
            )
            if _tc_m:
                _base = _tc_m.group(1)
                if _base.lower().startswith("synth"):
                    track_condition_disp = "Synth"
                else:
                    track_condition_disp = f"{_base.title()}{_tc_m.group(2) or ''}"
            else:
                track_condition_disp = track_condition_raw[:18]
        # Use m.code per meeting so All (AU) mixed-code roster works.
        row_code = getattr(m, "code", None) or code or "greyhound"
        state_code = (getattr(m, "extra", {}) or {}).get("state") or ""
        country_roster = (getattr(m, "extra", {}) or {}).get("country") or "AU"
        venue_disp = getattr(m, "venue", "") or ""
        if row_code == "thoroughbred" and state_code:
            venue_disp = f"{venue_disp} ({state_code})"

        per_race_m = timedelta(minutes=25 if row_code == "greyhound" else 35 if row_code == "thoroughbred" else 30)
        mtg_tz = _tz_for_tb_meeting(m) if row_code == "thoroughbred" else _tz_for_greyhound_meeting(m) if row_code == "greyhound" else None
        if mtg_tz is None and (getattr(m, "extra", {}) or {}).get("country") == "NZ":
            mtg_tz = ZoneInfo("Pacific/Auckland")
        if mtg_tz is None and app_tz is not None:
            mtg_tz = app_tz
        runners_by_race = mf.get("runners_by_race") or {}
        for r in races:
            runners_race = runners_by_race.get(getattr(r, "race_no"), []) or []
            # Field size = count of non-scratched runners so it matches actual field (scratches often not on initial acceptances)
            if runners_by_race is not None and runners_race:
                _scratched = lambda x: getattr(x, "scratched", False) if not isinstance(x, dict) else bool(x.get("scratched"))
                field_size = sum(1 for rr in runners_race if not _scratched(rr))
            else:
                field_size = len(runners_race) if runners_by_race is not None else ""
            start_t = getattr(r, "start_time_local", None)
            dt = None
            approx = False
            if isinstance(start_t, time):
                # Convert meeting-local time -> app timezone for correct national ordering.
                dt_local = datetime.combine(chosen_date, start_t, tzinfo=(mtg_tz or now.tzinfo))
                dt = dt_local.astimezone(app_tz) if app_tz is not None else dt_local
            elif row_code == "greyhound":
                # thedogs/GRNZ times are in track local time; use mtg_tz then convert to app_tz.
                first_t = getattr(m, "first_race_time_local", None)
                rn = getattr(r, "race_no", None)
                if isinstance(first_t, time) and isinstance(rn, int) and rn >= 1:
                    dt = datetime.combine(chosen_date, first_t, tzinfo=(mtg_tz or now.tzinfo)) + per_race_m * (rn - 1)
                    if app_tz is not None:
                        dt = dt.astimezone(app_tz)
                    approx = True
                    used_approx = True

            status = "unknown"
            if dt is not None:
                if now < dt:
                    status = "upcoming"
                elif now <= dt + per_race_m:
                    status = "in_progress"
                else:
                    status = "finished"

            _code_to_type = {"thoroughbred": "Thoroughbred", "greyhound": "Greyhound", "harness": "Harness"}
            _code_to_icon = {"thoroughbred": "T", "greyhound": "D", "harness": "H"}
            type_display = _code_to_type.get((row_code or "").lower(), (row_code or "").capitalize() or "—")
            type_icon = _code_to_icon.get((row_code or "").lower(), "—")
            race_name = str(getattr(r, "name", "") or "")
            class_label = ""
            if row_code == "thoroughbred":
                class_label = str((getattr(r, "extra", {}) or {}).get("class_label") or "") or parse_race_class_label(race_name)
            row = {
                "venue": venue_disp,
                "type": type_icon,
                "_type_label": type_display,  # full name for tooltips/accessibility if needed
                "race_no": getattr(r, "race_no", None),
                "race": f"R{getattr(r, 'race_no', '')}",
                "class": class_label,
                "track": track_condition_disp,
                "time": (
                    (f"~{dt.strftime('%H:%M')}" if approx and dt is not None else (dt.strftime("%H:%M") if dt is not None else ""))
                    + (
                        f" ({state_code} {start_t.strftime('%H:%M')})"
                        if (
                            row_code == "thoroughbred"
                            and state_code
                            and isinstance(start_t, time)
                            and app_tz is not None
                            and mtg_tz is not None
                            and mtg_tz != app_tz
                        )
                        else ""
                    )
                ),
                "status": status,
                "race_length": f"{getattr(r, 'distance_m', None)}m" if getattr(r, "distance_m", None) is not None else "—",
                "race_name": race_name,
                "best_pick": "",
                "best_pick_no": "",
                "if_scratched": "",
                "if_scratched_no": "",
                "roughie": "",
                "roughie_no": "",
                "why": "",
                "_best_pick_why": [],
                "_backup_pick_why": [],
                "race_link": getattr(r, "race_url", ""),
                "meeting_link": getattr(m, "meeting_url", ""),
                "_code": row_code,  # for All (AU) pick dispatch
                "_source": getattr(m, "source", ""),  # e.g. grnz_nz so we don't fetch GRNZ URLs for runners
                "country": country_roster,
                "dt": dt,  # for Sky overlay delta_minutes
                "field_size": field_size,  # number of runners in field (0 when not loaded)
            }
            rows.append((dt, row))

            if dt is not None and now < dt:
                if next_race is None or dt < next_race[0]:
                    next_race = (dt, row)

            # Important: never stop building the roster.
            # Once we hit the pick_limit, we simply stop computing picks (best_pick stays blank),
            # but we continue collecting all races across all venues for the day.

    if not rows:
        st.info("No races loaded yet for this date. Wait for the auto-load, or change date/code.")
        return

    # Full card: keep all races for the day (including earlier finished). Optional hide-all-finished only.
    if not show_finished:
        rows = [(dt, r) for dt, r in rows if (r.get("status") or "") != "finished"]

    # Chronological full card: earliest race first so previous races stay visible above later ones.
    def sort_key(item):
        dt, r = item
        if dt is None:
            return (True, 0, r.get("venue") or "", r.get("race") or "")
        return (False, dt.timestamp(), r.get("venue") or "", r.get("race") or "")

    rows_sorted = [r for _, r in sorted(rows, key=sort_key)]

    # Apply type filter (T / H / D) when in All mode
    if code_label in ("All (AU)", "All (AU+NZ)"):
        enabled_types = []
        if st.session_state.get("roster_show_t", True):
            enabled_types.append("thoroughbred")
        if st.session_state.get("roster_show_h", True):
            enabled_types.append("harness")
        if st.session_state.get("roster_show_d", True):
            enabled_types.append("greyhound")
        if enabled_types:
            rows_sorted = [r for r in rows_sorted if (r.get("_code") or "").lower() in enabled_types]
        # If all three unchecked, show none (empty list)

    # Compute best picks for all displayed rows (pick_limit 0 = no limit). Cache by (code, date, refresh) so grid click doesn't recompute.
    _PICK_CACHE_KEYS = (
        "best_pick", "best_pick_no", "best_pick_silk", "best_pick_detail", "best_pick_score",
        "if_scratched", "if_scratched_no", "if_scratched_silk", "if_scratched_detail", "if_scratched_score",
        "just_place", "just_place_no", "just_place_silk", "just_place_detail", "just_place_score",
        "roughie", "roughie_no", "roughie_silk", "roughie_detail", "roughie_score",
        "why", "_best_pick_why", "_backup_pick_why", "_just_place_why",
    )
    if show_best_pick and rows_sorted:
        cache_key = (
            code_label,
            chosen_date.isoformat(),
            st.session_state.get("roster_loaded_refresh", -1),
            "pickdetail_v2",
        )
        full_cache = st.session_state.get("roster_picks_cache") or {}
        row_cache = full_cache.get(cache_key) or {}
        row_ids = [(rr.get("meeting_link"), rr.get("race_no")) for rr in rows_sorted]
        all_cached = row_cache and all(rid in row_cache for rid in row_ids)
        did_recompute = False
        if all_cached:
            for rr in rows_sorted:
                rid = (rr.get("meeting_link"), rr.get("race_no"))
                if rid in row_cache:
                    rr.update(row_cache[rid])
        else:
            did_recompute = True
            prog = st.progress(0)
            first_error = None
            total_rows = len(rows_sorted)
            with st.spinner("Computing best picks..."):
                for idx, rr in enumerate(rows_sorted):
                    if pick_limit > 0 and computed_picks >= pick_limit:
                        break
                    rid = (rr.get("meeting_link"), rr.get("race_no"))
                    if rid in row_cache:
                        rr.update(row_cache[rid])
                        prog.progress(min(100, int((idx + 1) / max(total_rows, 1) * 100)))
                        continue
                    try:
                        row_code = rr.get("_code") or code
                        race_link_rr = str(rr.get("race_link") or "")
                        if row_code == "greyhound":
                            if "grnz.co.nz" in race_link_rr or rr.get("_source") == "grnz_nz":
                                mf = fields_by_meeting.get(str(rr.get("meeting_link") or ""), {}) or {}
                                runners_by = mf.get("runners_by_race") or {}
                                rn = rr.get("race_no")
                                runners = runners_by.get(rn, []) if rn is not None else []
                            else:
                                runners = cached_dog_runners(race_link_rr)
                        else:
                            mf = fields_by_meeting.get(str(rr.get("meeting_link") or ""), {}) or {}
                            runners_by = mf.get("runners_by_race") or {}
                            rn = rr.get("race_no")
                            runners = runners_by.get(rn, []) if rn is not None else []
                        if runners:
                            # Exclude scratched runners if present in this code/source.
                            runners = [x for x in runners if not bool(getattr(x, "scratched", False))]
                            if runners:
                                bw, fw, ew, _rat = suggest_auto_weights(runners, weather=None, track_condition=None)
                                ranked = rank_runners(
                                    runners,
                                    box_weight=bw,
                                    form_weight=fw,
                                    early_weight=ew,
                                    weather=None,
                                    track_condition=None,
                                    explain_mode="short",
                                )
                                if ranked:
                                    rr["best_pick"] = ranked[0].name
                                    rr["best_pick_score"] = float(getattr(ranked[0], "score", 0.0) or 0.0)
                                    rr["_best_pick_why"] = list(ranked[0].why_bullets)[:6]
                                    rr["best_pick_no"] = _runner_number_for_name(runners, ranked[0].name)
                                    rr["best_pick_silk"] = _silk_url_for_name(runners, ranked[0].name)
                                    rr["best_pick_detail"] = _pick_detail_json(
                                        runners,
                                        role="Best pick",
                                        name=ranked[0].name,
                                        no=rr["best_pick_no"],
                                        silk=rr["best_pick_silk"],
                                        why_bullets=rr["_best_pick_why"],
                                        race_row=rr,
                                    )
                                    if len(ranked) >= 2:
                                        rr["if_scratched"] = ranked[1].name
                                        rr["if_scratched_score"] = float(getattr(ranked[1], "score", 0.0) or 0.0)
                                        rr["_backup_pick_why"] = list(ranked[1].why_bullets)[:6]
                                        rr["if_scratched_no"] = _runner_number_for_name(runners, ranked[1].name)
                                        rr["if_scratched_silk"] = _silk_url_for_name(runners, ranked[1].name)
                                        rr["if_scratched_detail"] = _pick_detail_json(
                                            runners,
                                            role="If scratched",
                                            name=ranked[1].name,
                                            no=rr["if_scratched_no"],
                                            silk=rr["if_scratched_silk"],
                                            why_bullets=rr["_backup_pick_why"],
                                            race_row=rr,
                                        )
                                    # Just place: place-market tip (not the win tip).
                                    # Data check: when R1−R2 gap is clear (≥0.05), rank 2 placed more often;
                                    # when the race is tight, rank 1 is the safer place.
                                    place_r = ranked[0]
                                    place_note = "Clear favourite also for place"
                                    if len(ranked) >= 2:
                                        gap12 = float(getattr(ranked[0], "score", 0.0) or 0.0) - float(
                                            getattr(ranked[1], "score", 0.0) or 0.0
                                        )
                                        if gap12 >= 0.05:
                                            place_r = ranked[1]
                                            place_note = f"Place angle (gap {gap12:.2f} — rank 2)"
                                        else:
                                            place_note = f"Tight race (gap {gap12:.2f}) — favour place on rank 1"
                                    rr["just_place"] = place_r.name
                                    rr["just_place_score"] = float(getattr(place_r, "score", 0.0) or 0.0)
                                    rr["_just_place_why"] = [place_note] + list(place_r.why_bullets)[:5]
                                    rr["just_place_no"] = _runner_number_for_name(runners, place_r.name)
                                    rr["just_place_silk"] = _silk_url_for_name(runners, place_r.name)
                                    rr["just_place_detail"] = _pick_detail_json(
                                        runners,
                                        role="Just place",
                                        name=place_r.name,
                                        no=rr["just_place_no"],
                                        silk=rr["just_place_silk"],
                                        why_bullets=rr["_just_place_why"],
                                        race_row=rr,
                                    )
                                    # Roughie = last-ranked (long-shot) pick
                                    rr["roughie"] = ranked[-1].name
                                    rr["roughie_score"] = float(getattr(ranked[-1], "score", 0.0) or 0.0)
                                    rr["roughie_no"] = _runner_number_for_name(runners, ranked[-1].name)
                                    rr["roughie_silk"] = _silk_url_for_name(runners, ranked[-1].name)
                                    rr["roughie_detail"] = _pick_detail_json(
                                        runners,
                                        role="Roughie",
                                        name=ranked[-1].name,
                                        no=rr["roughie_no"],
                                        silk=rr["roughie_silk"],
                                        why_bullets=list(ranked[-1].why_bullets)[:6],
                                        race_row=rr,
                                    )
                                    # Inline rationale summary for the row.
                                    kf = getattr(ranked[0], "key_factors", "") or ""
                                    if not kf:
                                        kf = "; ".join([b.strip("- ").strip() for b in rr["_best_pick_why"] if b])[:180]
                                    rr["why"] = _truncate(kf, 110)
                                    computed_picks += 1
                                    row_cache[rid] = {k: rr.get(k) for k in _PICK_CACHE_KEYS}
                    except Exception as e:
                        if first_error is None:
                            first_error = e
                    prog.progress(min(100, int((idx + 1) / max(total_rows, 1) * 100)))
            full_cache[cache_key] = row_cache
            st.session_state.roster_picks_cache = full_cache
            prog.empty()
            if first_error is not None:
                st.warning(f"Some races failed to rank (first error: {first_error!s}). Check field data is loaded.")

        # Auto-save picks for next-day Daily review (once per picks cache key, or after recompute).
        autosave_key = (*cache_key, "autosave_v3")
        if did_recompute or st.session_state.get("roster_autosaved_key") != autosave_key:
            n_saved = autosave_roster_picks(chosen_date, rows_sorted)
            st.session_state.roster_autosaved_key = autosave_key
            if n_saved:
                st.caption(
                    f"Auto-saved **{n_saved}** picks for **{chosen_date.isoformat()}** "
                    "(open **Daily review** tomorrow to check winners vs picks)."
                )
    # Defaults: Grid (AG Grid) when available; overlay Sky schedule on. No UI for these.
    mode = "Grid (AG Grid)" if _AGGRID_AVAILABLE else ("Table (faster for large lists)" if len(rows_sorted) > 150 else "Interactive rows (WHY popup)")
    overlay_sky_roster = st.session_state.get("roster_overlay_sky", True)

    # Highlight the race that is jumping now, or the next one up.
    # Do NOT use the long "in_progress" status window (~35m for TB) — that left the
    # amber row stuck ~half an hour behind wall-clock.
    def _live_window_for_row(r: dict) -> timedelta:
        code_r = (r.get("_code") or "").lower()
        if code_r == "greyhound":
            return timedelta(minutes=5)
        if code_r == "harness":
            return timedelta(minutes=8)
        return timedelta(minutes=8)  # thoroughbred / default

    current_race_id: tuple | None = None
    current_race_kind = ""
    live_candidates = []
    upcoming_candidates = []
    finished_candidates = []
    for r in rows_sorted:
        dt_r = r.get("dt")
        if dt_r is None:
            continue
        if dt_r <= now <= dt_r + _live_window_for_row(r):
            live_candidates.append((dt_r, r))
        elif dt_r > now:
            upcoming_candidates.append((dt_r, r))
        elif dt_r < now:
            finished_candidates.append((dt_r, r))

    if live_candidates:
        # Most recently jumped among races still in the short live window.
        cur = max(live_candidates, key=lambda x: x[0])[1]
        current_race_id = (cur.get("meeting_link"), cur.get("race_no"))
        current_race_kind = "live"
    elif upcoming_candidates:
        cur = min(upcoming_candidates, key=lambda x: x[0])[1]
        current_race_id = (cur.get("meeting_link"), cur.get("race_no"))
        current_race_kind = "next"
    elif next_race is not None:
        cur = next_race[1]
        current_race_id = (cur.get("meeting_link"), cur.get("race_no"))
        current_race_kind = "next"
    elif finished_candidates:
        cur = max(finished_candidates, key=lambda x: x[0])[1]
        current_race_id = (cur.get("meeting_link"), cur.get("race_no"))
        current_race_kind = "last"

    current_race_label = ""
    if current_race_id is not None:
        for r in rows_sorted:
            if (r.get("meeting_link"), r.get("race_no")) == current_race_id:
                tdisp = (r.get("time") or "").strip()
                kind = current_race_kind or r.get("status") or "current"
                current_race_label = f"{r.get('venue') or ''} {r.get('race') or ''} {tdisp} ({kind})".strip()
                break

    # --- Jump alerts (live countdown) + Scratchings board ---
    def _collect_field_scratchings() -> list[dict]:
        out: list[dict] = []
        for r in rows_sorted:
            if (r.get("status") or "") == "finished":
                continue
            runners = _runners_objects_for_roster_row(r)
            tips = {
                (r.get("best_pick") or "").strip().lower(),
                (r.get("if_scratched") or "").strip().lower(),
                (r.get("just_place") or "").strip().lower(),
            }
            tips.discard("")
            for runner in runners or []:
                if not bool(getattr(runner, "scratched", False)):
                    continue
                name = (getattr(runner, "name", None) or "").strip()
                if not name:
                    continue
                hit = name.lower() in tips
                which = []
                if name.lower() == (r.get("best_pick") or "").strip().lower():
                    which.append("best")
                if name.lower() == (r.get("if_scratched") or "").strip().lower():
                    which.append("backup")
                if name.lower() == (r.get("just_place") or "").strip().lower():
                    which.append("place")
                out.append(
                    {
                        "venue": r.get("venue") or "",
                        "race": r.get("race") or "",
                        "race_no": r.get("race_no"),
                        "time": r.get("time") or "",
                        "horse": name,
                        "no": _runner_number_for_name(runners, name),
                        "tip_hit": ", ".join(which) if which else "",
                        "source": "field",
                    }
                )
        return out

    field_scratches = _collect_field_scratchings()
    sb_scratches_raw = (
        cached_sportsbet_scratchings(chosen_date)
        if (code_label.startswith("Thoroughbred") or code_label in ("All (AU)", "All (AU+NZ)"))
        else []
    )
    # Merge Sportsbet outs not already listed from field data.
    seen_scr = {
        (
            re.sub(r"\s*\([^)]*\)\s*$", "", str(s.get("venue") or "")).strip().lower(),
            int(s["race_no"]) if s.get("race_no") is not None else -1,
            _odds_norm_horse(str(s.get("horse") or "")),
        )
        for s in field_scratches
        if s.get("horse")
    }
    tip_by_vr: dict[tuple[str, int], dict] = {}
    for r in rows_sorted:
        try:
            rn = int(r.get("race_no"))
        except Exception:
            continue
        vk = re.sub(r"\s*\([^)]*\)\s*$", "", str(r.get("venue") or "")).strip().lower()
        tip_by_vr[(vk, rn)] = r
    merged_scratches = list(field_scratches)
    for s in sb_scratches_raw:
        vk = re.sub(r"\s*\([^)]*\)\s*$", "", str(s.get("venue") or "")).strip().lower()
        try:
            rn = int(s.get("race_no"))
        except Exception:
            continue
        key = (vk, rn, _odds_norm_horse(str(s.get("horse") or "")))
        if not key[2] or key in seen_scr:
            continue
        seen_scr.add(key)
        row = tip_by_vr.get((vk, rn)) or {}
        name = str(s.get("horse") or "")
        which = []
        if name.lower() == (row.get("best_pick") or "").strip().lower():
            which.append("best")
        if name.lower() == (row.get("if_scratched") or "").strip().lower():
            which.append("backup")
        if name.lower() == (row.get("just_place") or "").strip().lower():
            which.append("place")
        merged_scratches.append(
            {
                "venue": s.get("venue") or row.get("venue") or "",
                "race": row.get("race") or (f"R{rn}" if rn else ""),
                "race_no": rn,
                "time": row.get("time") or "",
                "horse": name,
                "no": str(s.get("no") or "")
                or (
                    _runner_number_for_name(_runners_objects_for_roster_row(row), name) if row else ""
                ),
                "tip_hit": ", ".join(which),
                "source": "sportsbet",
            }
        )
    merged_scratches.sort(
        key=lambda x: (
            str(x.get("time") or "99:99"),
            str(x.get("venue") or ""),
            int(x.get("race_no") or 0),
            str(x.get("horse") or ""),
        )
    )
    tip_hits = [s for s in merged_scratches if s.get("tip_hit")]

    # Jump board payload for live client ticker (next ~60 minutes).
    jump_payload = []
    for dt_r, r in sorted(upcoming_candidates, key=lambda x: x[0])[:40]:
        try:
            jump_payload.append(
                {
                    "venue": r.get("venue") or "",
                    "race": r.get("race") or "",
                    "time": r.get("time") or "",
                    "dt_ms": int(dt_r.timestamp() * 1000),
                    "pick": r.get("best_pick") or "",
                    "code": r.get("_code") or "",
                }
            )
        except Exception:
            continue

    import streamlit.components.v1 as components
    import json as _json

    jump_html = f"""
<div id="jump-alerts" style="font:13px/1.4 -apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;color:#f2f2f2;background:linear-gradient(90deg,#2a2118,#1e1e1e);border:1px solid #6b542e;border-radius:10px;padding:10px 14px;">
  <div style="display:flex;justify-content:space-between;gap:12px;align-items:baseline;margin-bottom:6px;">
    <div style="font-size:11px;letter-spacing:0.06em;text-transform:uppercase;color:#d4b37a;font-weight:650;">Jump alerts</div>
    <div id="jump-clock" style="color:#9aa0a6;font-size:11px;"></div>
  </div>
  <div id="jump-live" style="font-weight:650;margin-bottom:4px;">Loading…</div>
  <div id="jump-next" style="color:#c8c8c8;font-size:12px;"></div>
</div>
<script>
(function() {{
  var races = {_json.dumps(jump_payload, ensure_ascii=False)};
  function esc(s) {{
    return String(s == null ? '' : s).replace(/</g, '&lt;');
  }}
  function fmtMins(ms) {{
    var m = Math.round(ms / 60000);
    if (m <= 0) return 'NOW';
    if (m === 1) return '1 min';
    return m + ' min';
  }}
  function tick() {{
    var now = Date.now();
    var liveEl = document.getElementById('jump-live');
    var nextEl = document.getElementById('jump-next');
    var clockEl = document.getElementById('jump-clock');
    if (clockEl) clockEl.textContent = new Date(now).toLocaleTimeString([], {{hour:'2-digit', minute:'2-digit', second:'2-digit'}});
    if (!races.length) {{
      if (liveEl) liveEl.textContent = 'No upcoming race times loaded.';
      if (nextEl) nextEl.textContent = '';
      return;
    }}
    var soon = [];
    var later = [];
    for (var i = 0; i < races.length; i++) {{
      var r = races[i];
      var dt = Number(r.dt_ms);
      if (!isFinite(dt)) continue;
      var delta = dt - now;
      if (delta < -2 * 60000) continue; // jumped >2m ago
      var item = {{r: r, delta: delta}};
      if (delta <= 20 * 60000) soon.push(item);
      else if (delta <= 60 * 60000) later.push(item);
    }}
    soon.sort(function(a,b) {{ return a.delta - b.delta; }});
    later.sort(function(a,b) {{ return a.delta - b.delta; }});
    if (liveEl) {{
      if (!soon.length) {{
        liveEl.innerHTML = '<span style="color:#9aa0a6;">Nothing jumping in the next 20 minutes.</span>';
      }} else {{
        liveEl.innerHTML = soon.slice(0, 6).map(function(x) {{
          var urgent = x.delta <= 5 * 60000;
          var col = urgent ? '#ffb4a2' : '#f2f2f2';
          var pick = x.r.pick ? (' · tip ' + esc(x.r.pick)) : '';
          return '<span style="color:' + col + ';margin-right:14px;"><b>' + esc(fmtMins(x.delta)) + '</b> '
            + esc(x.r.venue) + ' ' + esc(x.r.race) + ' <span style="color:#9aa0a6;">' + esc(x.r.time) + '</span>'
            + pick + '</span>';
        }}).join('');
      }}
    }}
    if (nextEl) {{
      if (!later.length) {{ nextEl.textContent = ''; return; }}
      nextEl.innerHTML = 'Then: ' + later.slice(0, 5).map(function(x) {{
        return esc(x.r.venue) + ' ' + esc(x.r.race) + ' (' + esc(fmtMins(x.delta)) + ')';
      }}).join(' · ');
    }}
  }}
  tick();
  setInterval(tick, 5000);
}})();
</script>
"""
    components.html(jump_html, height=96)

    scr_label = f"Scratchings board ({len(merged_scratches)})"
    if tip_hits:
        scr_label += f" · ⚠ {len(tip_hits)} hit our tip"
    with st.expander(scr_label, expanded=bool(tip_hits) or bool(merged_scratches)):
        if tip_hits:
            st.warning(
                "Our tip is scratched: "
                + "; ".join(
                    f"{t.get('venue')} {t.get('race')} — {t.get('horse')} ({t.get('tip_hit')})"
                    for t in tip_hits[:8]
                )
            )
        if not merged_scratches:
            st.caption(
                "No scratchings detected yet for remaining races. "
                "Sources: field Acceptances (SCR) + Sportsbet late outs. Refresh after late changes."
            )
        else:
            st.caption(
                "Field = Acceptances SCR · Sportsbet = late market outs. "
                "**tip_hit** = scratched horse was our best / backup / place tip."
            )
            st.dataframe(
                [
                    {
                        "venue": s.get("venue"),
                        "race": s.get("race"),
                        "time": s.get("time"),
                        "no": s.get("no"),
                        "horse": s.get("horse"),
                        "tip_hit": s.get("tip_hit") or "",
                        "source": s.get("source"),
                    }
                    for s in merged_scratches
                ],
                width="stretch",
                hide_index=True,
            )

    def _normalize_venue_sky_roster(v: str) -> str:
        s = (v or "").strip()
        s = re.sub(r"\s*\([^)]+\)\s*$", "", s).strip()
        return s.lower()

    if mode.startswith("Table") or (mode.startswith("Grid") and _AGGRID_AVAILABLE):
        display_rows = []
        for r in rows_sorted:
            runners_for_picks = _runners_objects_for_roster_row(r)
            today_cls = str(r.get("class") or "")
            venue_r = str(r.get("venue") or "")
            race_no_r = r.get("race_no")
            pick = r.get("best_pick", "")
            pick_no = r.get("best_pick_no", "")
            pick_disp = _format_pick_cell(
                name=pick,
                no=pick_no,
                runners=runners_for_picks,
                today_class=today_cls,
                odds_suffix=_odds_suffix_for(venue_r, race_no_r, pick),
            )
            bkup = r.get("if_scratched", "")
            bkup_no = r.get("if_scratched_no", "")
            bkup_disp = _format_pick_cell(
                name=bkup,
                no=bkup_no,
                runners=runners_for_picks,
                today_class=today_cls,
                prefix="→ ",
                odds_suffix=_odds_suffix_for(venue_r, race_no_r, bkup),
            )
            place = r.get("just_place", "")
            place_no = r.get("just_place_no", "")
            place_disp = _format_pick_cell(
                name=place,
                no=place_no,
                runners=runners_for_picks,
                today_class=today_cls,
                odds_suffix=_odds_suffix_for(venue_r, race_no_r, place),
            )
            rough = r.get("roughie", "")
            rough_no = r.get("roughie_no", "")
            rough_disp = _format_pick_cell(
                name=rough,
                no=rough_no,
                runners=runners_for_picks,
                today_class=today_cls,
                odds_suffix=_odds_suffix_for(venue_r, race_no_r, rough),
            )
            fs = r.get("field_size")
            _dt = r.get("dt")
            try:
                dt_ms = int(_dt.timestamp() * 1000) if _dt is not None else None
            except Exception:
                dt_ms = None
            display_rows.append(
                {
                    "venue": r.get("venue", ""),
                    "type": r.get("type", ""),
                    "race": r.get("race", ""),
                    "class": r.get("class", "") or "",
                    "track": r.get("track", "") or "",
                    "time": r.get("time", ""),
                    "race length": r.get("race_length", "—"),
                    "field size": str(fs) if fs is not None else "",
                    "best_pick": pick_disp,
                    "if_scratched": bkup_disp,
                    "just_place": place_disp,
                    "roughie": rough_disp,
                    "best_pick_silk": r.get("best_pick_silk") or "",
                    "if_scratched_silk": r.get("if_scratched_silk") or "",
                    "just_place_silk": r.get("just_place_silk") or "",
                    "roughie_silk": r.get("roughie_silk") or "",
                    "best_pick_detail": _patch_detail_odds(
                        r.get("best_pick_detail") or "", venue_r, race_no_r, pick
                    ),
                    "if_scratched_detail": _patch_detail_odds(
                        r.get("if_scratched_detail") or "", venue_r, race_no_r, bkup
                    ),
                    "just_place_detail": _patch_detail_odds(
                        r.get("just_place_detail") or "", venue_r, race_no_r, place
                    ),
                    "roughie_detail": _patch_detail_odds(
                        r.get("roughie_detail") or "", venue_r, race_no_r, rough
                    ),
                    "field_json": _field_grid_json(r),
                    "meeting_link": str(r.get("meeting_link") or ""),
                    "race_no": r.get("race_no"),
                    "_code": str(r.get("_code") or ""),
                    # Absolute epoch ms for client-side highlight ticker (browser local clock).
                    "dt_ms": dt_ms if dt_ms is not None else "",
                    # int 1/0 survives Arrow/JSON better than bool for AG Grid rules
                    "is_current": int(
                        current_race_id is not None
                        and (r.get("meeting_link"), r.get("race_no")) == current_race_id
                    ),
                    # Kept for Ctrl+click Why popup (not shown as a grid column).
                    "why": r.get("why", ""),
                    "_r": r,
                }
            )
        if overlay_sky_roster:
            sky_list_roster = cached_sky_schedule(chosen_date)
            sky_by_key_roster: dict[tuple[str, str, int], dict] = {}
            for s in sky_list_roster:
                ven = (s.get("venue") or "").strip()
                rn = s.get("race_no")
                if rn is None:
                    continue
                try:
                    rn = int(rn)
                except (TypeError, ValueError):
                    continue
                key = ("AU", _normalize_venue_sky_roster(ven), rn)
                if key not in sky_by_key_roster:
                    sky_by_key_roster[key] = s
            for d in display_rows:
                r = d.get("_r") or {}
                country = (r.get("country") or "AU").strip()
                venue = (r.get("venue") or "").strip()
                rno = r.get("race_no")
                try:
                    rno_int = int(rno) if rno is not None else None
                except (TypeError, ValueError):
                    rno_int = None
                d["sky_channel"] = ""
                d["sky_dt"] = ""
                d["delta_minutes"] = ""
                d["delta_note"] = ""
                if rno_int is not None:
                    key1 = (country, _normalize_venue_sky_roster(venue), rno_int)
                    sky = sky_by_key_roster.get(key1) or (sky_by_key_roster.get(("AU", _normalize_venue_sky_roster(venue), rno_int)) if country == "AU" else None)
                    if sky:
                        d["sky_channel"] = str(sky.get("channel", ""))
                        dt_sky = sky.get("dt_app_tz") or sky.get("dt_local")
                        d["sky_dt"] = dt_sky.strftime("%H:%M") if dt_sky else "—"
                        our_dt = r.get("dt")
                        delta_min = None
                        if our_dt is not None and dt_sky is not None:
                            delta_min = round((our_dt - dt_sky).total_seconds() / 60)
                        d["delta_minutes"] = str(delta_min) if delta_min is not None else "—"
                        d["delta_note"] = "⚠ ≥2 min" if delta_min is not None and abs(delta_min) >= 2 else ""
                d.pop("_r", None)
        else:
            for d in display_rows:
                d.pop("_r", None)
        # Sky overlay data is still merged for internal use; we no longer show sky columns in the grid.
        roster_cols = [
            "venue",
            "type",
            "race",
            "class",
            "track",
            "time",
            "race length",
            "field size",
            "best_pick",
            "if_scratched",
            "just_place",
            "roughie",
        ]
        # Column config so columns have explicit widths and are user-resizable (Streamlit dataframe supports resize).
        _roster_widths = {
            "venue": "large",
            "type": "small",
            "race": "small",
            "class": "small",
            "track": "small",
            "time": "small",
            "race length": "small",
            "field size": "small",
            "best_pick": "medium",
            "if_scratched": "medium",
            "just_place": "medium",
            "roughie": "medium",
        }
        roster_column_config = {
            col: st.column_config.Column(col.replace("_", " ").title(), width=_roster_widths.get(col, "medium"))
            for col in roster_cols
        }
        if mode.startswith("Grid"):
            import pandas as pd
            from st_aggrid import DataReturnMode, GridUpdateMode
            silk_cols = [
                "best_pick_silk",
                "if_scratched_silk",
                "just_place_silk",
                "roughie_silk",
                "best_pick_detail",
                "if_scratched_detail",
                "just_place_detail",
                "roughie_detail",
                "field_json",
                "meeting_link",
                "race_no",
                "_code",
                "dt_ms",
                "is_current",
                "why",
            ]
            # No row selection — selectionChanged would Streamlit-rerun the whole page.
            _grid_widths = {
                "venue": 140,
                "type": 50,
                "race": 50,
                "class": 70,
                "track": 72,
                "time": 130,
                "race length": 75,
                "field size": 70,
                "best_pick": 210,
                "if_scratched": 210,
                "just_place": 200,
                "roughie": 200,
            }
            # Plain text in cells; silks via AG Grid JS class renderer (init/getGui).
            # Right-click opens a client-side detail panel (enlarged silk option + runner info).
            df = pd.DataFrame(display_rows)[roster_cols + silk_cols]
            gb = GridOptionsBuilder.from_dataframe(df)
            silk_renderer = (
                JsCode(
                    r"""
class SilkCellRenderer {
  init(params) {
    var self = this;
    this.eGui = document.createElement('span');
    this.eGui.style.display = 'inline-flex';
    this.eGui.style.alignItems = 'center';
    this.eGui.style.gap = '6px';
    this.eGui.style.cursor = 'context-menu';
    var field = (params.colDef && params.colDef.field) ? params.colDef.field : '';
    var silk = (params.data && params.data[field + '_silk']) ? String(params.data[field + '_silk']) : '';
    var text = (params.value == null) ? '' : String(params.value);
    var detailRaw = (params.data && params.data[field + '_detail']) ? String(params.data[field + '_detail']) : '';
    if (silk) {
      var img = document.createElement('img');
      img.src = silk;
      img.alt = '';
      img.style.height = '22px';
      img.style.width = 'auto';
      img.referrerPolicy = 'no-referrer';
      this.eGui.appendChild(img);
    }
    var label = document.createElement('span');
    label.textContent = text;
    this.eGui.appendChild(label);
    this.eGui.addEventListener('contextmenu', function(ev) {
      ev.preventDefault();
      ev.stopPropagation();
      self._openPickPopup(ev, field, text, silk, detailRaw, params.data || {});
    });
  }
  getGui() { return this.eGui; }
  _closePickPopup() {
    var old = document.getElementById('roster-pick-popup');
    if (old) old.remove();
    var backdrop = document.getElementById('roster-pick-popup-backdrop');
    if (backdrop) backdrop.remove();
  }
  _openPickPopup(ev, field, text, silk, detailRaw, row) {
    this._closePickPopup();
    var detail = {};
    try { detail = detailRaw ? JSON.parse(detailRaw) : {}; } catch (e) { detail = {}; }
    var roleMap = {best_pick: 'Best pick', if_scratched: 'If scratched', just_place: 'Just place', roughie: 'Roughie'};
    var role = detail.role || roleMap[field] || field;
    var name = detail.name || text || '';
    var no = detail.no || '';
    var silkUrl = detail.silk || silk || '';
    var why = Array.isArray(detail.why) ? detail.why : [];

    var backdrop = document.createElement('div');
    backdrop.id = 'roster-pick-popup-backdrop';
    backdrop.style.cssText = 'position:fixed;inset:0;z-index:9998;background:rgba(0,0,0,0.35);';
    var self = this;
    backdrop.addEventListener('click', function() { self._closePickPopup(); });

    var panel = document.createElement('div');
    panel.id = 'roster-pick-popup';
    panel.style.cssText = 'position:fixed;z-index:9999;min-width:280px;max-width:420px;max-height:80vh;overflow:auto;background:#1e1e1e;color:#f2f2f2;border:1px solid #555;border-radius:10px;padding:14px 16px;box-shadow:0 12px 40px rgba(0,0,0,0.45);font:13px/1.45 -apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;';
    var x = Math.min(ev.clientX, window.innerWidth - 440);
    var y = Math.min(ev.clientY, window.innerHeight - 80);
    if (x < 8) x = 8;
    if (y < 8) y = 8;
    panel.style.left = x + 'px';
    panel.style.top = y + 'px';

    function rowLine(k, v) {
      if (v === undefined || v === null || String(v).trim() === '') return '';
      return '<div style="display:flex;gap:8px;margin:3px 0;"><span style="color:#9aa0a6;min-width:88px;">' + k + '</span><span>' + String(v).replace(/</g,'&lt;') + '</span></div>';
    }
    function classRowHtml() {
      var today = detail.class_label || row['class'] || '';
      var arrow = detail.class_arrow || '';
      var last = detail.last_class || '';
      if (!today && !arrow && !last) return '';
      var color = arrow === '↑' ? '#f0a0a0' : (arrow === '↓' ? '#8fd19e' : '#c8c8c8');
      var tip = arrow === '↑' ? 'class up' : (arrow === '↓' ? 'class down' : (arrow === '→' ? 'same class' : ''));
      var html = String(today || '').replace(/</g,'&lt;');
      if (arrow) {
        html += ' <span style="color:' + color + ';font-weight:700;" title="' + tip + '">' + arrow + '</span>';
      }
      if (last) {
        html += ' <span style="color:#9aa0a6;">(last ' + String(last).replace(/</g,'&lt;') + ')</span>';
      }
      return '<div style="display:flex;gap:8px;margin:3px 0;"><span style="color:#9aa0a6;min-width:88px;">Class</span><span>' + html + '</span></div>';
    }

    var title = document.createElement('div');
    title.style.cssText = 'display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:10px;';
    title.innerHTML = '<div><div style="font-size:11px;color:#9aa0a6;text-transform:uppercase;letter-spacing:0.04em;">' + String(role).replace(/</g,'&lt;') + '</div><div style="font-size:16px;font-weight:650;">' + (no ? (String(no).replace(/</g,'&lt;') + '. ') : '') + String(name).replace(/</g,'&lt;') + '</div></div>';
    var closeBtn = document.createElement('button');
    closeBtn.textContent = '✕';
    closeBtn.style.cssText = 'background:transparent;border:0;color:#ccc;font-size:16px;cursor:pointer;';
    closeBtn.addEventListener('click', function() { self._closePickPopup(); });
    title.appendChild(closeBtn);
    panel.appendChild(title);

    var silkWrap = document.createElement('div');
    silkWrap.style.cssText = 'display:flex;align-items:center;gap:14px;margin:8px 0 12px 0;';
    var silkImg = document.createElement('img');
    silkImg.referrerPolicy = 'no-referrer';
    silkImg.alt = 'Jockey silks';
    silkImg.style.height = '110px';
    silkImg.style.width = 'auto';
    silkImg.style.imageRendering = 'auto';
    if (silkUrl) {
      silkImg.src = silkUrl;
      silkWrap.appendChild(silkImg);
    } else {
      var noSilk = document.createElement('div');
      noSilk.textContent = 'No silk image';
      noSilk.style.color = '#9aa0a6';
      silkWrap.appendChild(noSilk);
    }
    var enlargeLabel = document.createElement('label');
    enlargeLabel.style.cssText = 'display:inline-flex;align-items:center;gap:6px;cursor:pointer;user-select:none;';
    var enlargeCb = document.createElement('input');
    enlargeCb.type = 'checkbox';
    enlargeCb.checked = true;
    enlargeCb.addEventListener('change', function() {
      silkImg.style.height = enlargeCb.checked ? '110px' : '22px';
    });
    enlargeLabel.appendChild(enlargeCb);
    enlargeLabel.appendChild(document.createTextNode('Enlarge silk (5×)'));
    if (silkUrl) silkWrap.appendChild(enlargeLabel);
    panel.appendChild(silkWrap);

    var info = document.createElement('div');
    info.innerHTML =
      rowLine('Venue', detail.venue || row.venue || '') +
      rowLine('Race', detail.race || row.race || '') +
      classRowHtml() +
      rowLine('Track', detail.track || row.track || '') +
      rowLine('Time', detail.time || row.time || '') +
      rowLine('Distance', detail.distance || row['race length'] || '') +
      rowLine('Field', detail.field_size || row['field size'] || '') +
      rowLine('Barrier', detail.barrier || '') +
      rowLine('Jockey', detail.jockey || '') +
      rowLine('Trainer', detail.trainer || '') +
      rowLine('Age/Sex', ((detail.age || '') + (detail.sex ? (' ' + detail.sex) : '')).trim()) +
      rowLine('Weight', detail.weight || '') +
      rowLine('Rating', detail.benchmark || '') +
      rowLine('Odds', (detail.win_odds != null && detail.win_odds !== ''
        ? ('$' + detail.win_odds + (detail.fluc ? detail.fluc : '') +
           (detail.place_odds != null && detail.place_odds !== '' ? (' / plc $' + detail.place_odds) : '') +
           ' (Sportsbet)')
        : '')) +
      rowLine('Last 10', detail.last10 || '');
    panel.appendChild(info);

    if (why.length) {
      var whyTitle = document.createElement('div');
      whyTitle.textContent = 'Why';
      whyTitle.style.cssText = 'margin-top:10px;margin-bottom:4px;color:#9aa0a6;font-size:11px;text-transform:uppercase;letter-spacing:0.04em;';
      panel.appendChild(whyTitle);
      var ul = document.createElement('ul');
      ul.style.cssText = 'margin:0;padding-left:1.2em;';
      for (var i = 0; i < why.length; i++) {
        var li = document.createElement('li');
        li.textContent = String(why[i]);
        ul.appendChild(li);
      }
      panel.appendChild(ul);
    }

    if (detail.profile_url) {
      var link = document.createElement('a');
      link.href = detail.profile_url;
      link.target = '_blank';
      link.rel = 'noopener';
      link.textContent = 'Open horse form';
      link.style.cssText = 'display:inline-block;margin-top:12px;color:#8ab4f8;';
      panel.appendChild(link);
    }

    document.body.appendChild(backdrop);
    document.body.appendChild(panel);
  }
}
"""
                )
                if JsCode is not None
                else None
            )
            field_size_style = (
                JsCode(
                    r"""
function(params) {
  var raw = params.value;
  var n = parseInt(raw, 10);
  var base = { textAlign: 'center' };
  if (!isFinite(n) || n <= 0) return base;
  // AU place terms: <8 starters => no 3rd dividend (NTD for 5–7; <5 often no place market).
  if (n < 5) {
    return Object.assign(base, {
      backgroundColor: 'rgba(200, 60, 50, 0.65)',
      color: '#fff',
      fontWeight: '700',
      borderRadius: '4px'
    });
  }
  if (n < 8) {
    return Object.assign(base, {
      backgroundColor: 'rgba(230, 140, 40, 0.7)',
      color: '#1a1a1a',
      fontWeight: '700',
      borderRadius: '4px'
    });
  }
  return base;
}
"""
                )
                if JsCode is not None
                else None
            )
            field_size_renderer = (
                JsCode(
                    r"""
class FieldSizeCellRenderer {
  init(params) {
    this.eGui = document.createElement('span');
    this.eGui.style.display = 'inline-block';
    this.eGui.style.width = '100%';
    this.eGui.style.textAlign = 'center';
    var raw = params.value;
    var n = parseInt(raw, 10);
    var text = (raw == null || raw === '') ? '' : String(raw);
    if (isFinite(n) && n > 0 && n < 8) {
      text = n + (n < 5 ? ' · no place' : ' · NTD');
      this.eGui.title = n < 5
        ? 'Fewer than 5 starters — place betting usually not offered'
        : 'No third dividend (NTD): 5–7 starters — place pays 1st & 2nd only';
    } else if (isFinite(n) && n >= 8) {
      this.eGui.title = '8+ starters — place pays 1st, 2nd & 3rd';
    }
    this.eGui.textContent = text;
  }
  getGui() { return this.eGui; }
}
"""
                )
                if JsCode is not None
                else None
            )
            for col, w in _grid_widths.items():
                if silk_renderer is not None and col in ("best_pick", "if_scratched", "just_place", "roughie"):
                    gb.configure_column(col, width=w, cellRenderer=silk_renderer)
                elif col == "race":
                    gb.configure_column(
                        col,
                        width=w,
                        cellStyle={"cursor": "context-menu"},
                    )
                elif col == "field size":
                    fs_kwargs = {"width": max(w, 95)}
                    if field_size_style is not None:
                        fs_kwargs["cellStyle"] = field_size_style
                    if field_size_renderer is not None:
                        fs_kwargs["cellRenderer"] = field_size_renderer
                    # Classes + custom_css !important so NTD still shows on amber "current" rows.
                    fs_kwargs["cellClassRules"] = {
                        "roster-fs-ntd": "Number(value) >= 5 && Number(value) < 8",
                        "roster-fs-noplace": "Number(value) > 0 && Number(value) < 5",
                    }
                    gb.configure_column(col, **fs_kwargs)
                else:
                    gb.configure_column(col, width=w)
            for sc in silk_cols:
                gb.configure_column(sc, hide=True)
            should_return_race_ctx = (
                JsCode(
                    r"""
function({streamlitRerunEventTriggerName, eventData}) {
  if (streamlitRerunEventTriggerName !== 'cellContextMenu') return false;
  try {
    var field = null;
    if (eventData && eventData.colDef && eventData.colDef.field) field = eventData.colDef.field;
    else if (eventData && eventData.column && eventData.column.getColId) field = eventData.column.getColId();
    return field === 'race';
  } catch (e) { return false; }
}
"""
                )
                if JsCode is not None
                else None
            )
            collect_race_ctx = (
                JsCode(
                    r"""
function({streamlitRerunEventTriggerName, eventData}) {
  var data = (eventData && eventData.data) ? eventData.data : {};
  var raceNo = data.race_no;
  if (raceNo == null && data.race) {
    var m = String(data.race).match(/R?\s*(\d+)/i);
    if (m) raceNo = parseInt(m[1], 10);
  }
  return {
    action: 'race_results',
    venue: data.venue || '',
    race: data.race || '',
    race_no: raceNo,
    meeting_link: data.meeting_link || '',
    _code: data._code || '',
    nonce: Date.now()
  };
}
"""
                )
                if JsCode is not None
                else None
            )
            row_modifier_click = (
                JsCode(
                    r"""
function(params) {
  var ev = params.event;
  if (!ev) return;
  var isShift = !!ev.shiftKey;
  var isWhy = !!(ev.ctrlKey || ev.metaKey);
  if (!isShift && !isWhy) return;
  try { ev.preventDefault(); ev.stopPropagation(); } catch (e) {}

  function closeAllPopups() {
    ['roster-field-popup','roster-field-popup-backdrop','roster-pick-popup','roster-pick-popup-backdrop','roster-why-popup','roster-why-popup-backdrop'].forEach(function(id) {
      var el = document.getElementById(id);
      if (el) el.remove();
    });
  }
  closeAllPopups();

  function esc(s) {
    return String(s == null ? '' : s).replace(/</g, '&lt;');
  }

  if (isWhy) {
    var field = (params.colDef && params.colDef.field) ? params.colDef.field : '';

    // Ctrl/⌘+click Class → define this race grade
    if (field === 'class') {
      var classLabel = (params.data && params.data['class']) ? String(params.data['class']) : '';
      var venueC = (params.data && params.data.venue) ? String(params.data.venue) : '';
      var raceC = (params.data && params.data.race) ? String(params.data.race) : '';
      var raceNameC = '';
      try {
        var fj = (params.data && params.data.field_json) ? JSON.parse(String(params.data.field_json)) : {};
        if (fj && fj.class_label) classLabel = classLabel || String(fj.class_label);
      } catch (e) {}

      function classDefinition(lab) {
        var L = String(lab || '').trim().toUpperCase();
        if (!L) {
          return {
            title: 'Unknown class',
            blurb: 'Could not parse a class label from this race title.',
            ladder: true
          };
        }
        if (L === 'MDN' || L.indexOf('MAIDEN') >= 0) {
          return {
            title: 'Maiden (MDN)',
            blurb: 'For horses that have never won a race. Usually the lowest rung of the ladder (aside from trials).'
          };
        }
        var cl = L.match(/^CL\s*([1-6])$/);
        if (cl) {
          var n = parseInt(cl[1], 10);
          return {
            title: 'Class ' + n + ' (Cl' + n + ')',
            blurb: 'Restricted by wins. Rough guide: Cl1 ≈ one-win horses, up through Cl6 for more seasoned winners. Higher number = tougher than lower Class races.'
          };
        }
        var bm = L.match(/^BM\s*(\d+)$/);
        if (bm) {
          return {
            title: 'Benchmark ' + bm[1] + ' (BM' + bm[1] + ')',
            blurb: 'Handicap where official ratings sit around this mark. Higher BM = stronger field (e.g. BM58 easier than BM70). Weights are set from ratings.'
          };
        }
        if (L === 'OPEN') {
          return {
            title: 'Open handicap / Open',
            blurb: 'Open to a wide ratings band — usually stronger than mid Benchmarks / Class races, below Listed/Group.'
          };
        }
        if (L === 'LR' || L === 'LISTED') {
          return {
            title: 'Listed (LR)',
            blurb: 'Black-type race below Group level. Stronger than Open / high BM; stepping stone to Group racing.'
          };
        }
        var g = L.match(/^G\s*([123])$/);
        if (g) {
          return {
            title: 'Group ' + g[1] + ' (G' + g[1] + ')',
            blurb: 'Elite black-type. G1 is the highest, then G2, then G3. Much tougher than Benchmark / Class races.'
          };
        }
        if (L === 'TRIAL') {
          return {
            title: 'Trial',
            blurb: 'Barrier / jump-out style trial — not a TAB race for our picks ladder.'
          };
        }
        return {
          title: lab,
          blurb: 'Parsed from the race title. Ladder (roughly easier → tougher): MDN → Cl1–6 → BM58/64/… → OPEN → Listed → G3 → G2 → G1.'
        };
      }

      var def = classDefinition(classLabel);
      var backdrop = document.createElement('div');
      backdrop.id = 'roster-why-popup-backdrop';
      backdrop.style.cssText = 'position:fixed;inset:0;z-index:9998;background:rgba(0,0,0,0.4);';
      backdrop.addEventListener('click', closeAllPopups);

      var panel = document.createElement('div');
      panel.id = 'roster-why-popup';
      panel.style.cssText = 'position:fixed;z-index:9999;left:50%;top:10vh;transform:translateX(-50%);width:min(520px,94vw);max-height:80vh;overflow:auto;background:#1e1e1e;color:#f2f2f2;border:1px solid #555;border-radius:12px;padding:16px 18px;box-shadow:0 16px 48px rgba(0,0,0,0.5);font:13px/1.45 -apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;';

      var header = document.createElement('div');
      header.style.cssText = 'display:flex;justify-content:space-between;align-items:flex-start;gap:12px;margin-bottom:12px;';
      var titleWrap = document.createElement('div');
      titleWrap.innerHTML =
        '<div style="font-size:11px;color:#9aa0a6;text-transform:uppercase;letter-spacing:0.04em;">Race class</div>' +
        '<div style="font-size:20px;font-weight:650;margin-top:4px;">' + esc(def.title) + '</div>' +
        '<div style="color:#9aa0a6;margin-top:4px;">' + esc(venueC) + ' ' + esc(raceC) +
        (classLabel ? (' · label <b style="color:#f2f2f2;">' + esc(classLabel) + '</b>') : '') + '</div>';
      header.appendChild(titleWrap);
      var closeBtn = document.createElement('button');
      closeBtn.textContent = '✕';
      closeBtn.style.cssText = 'background:transparent;border:0;color:#ccc;font-size:18px;cursor:pointer;';
      closeBtn.addEventListener('click', closeAllPopups);
      header.appendChild(closeBtn);
      panel.appendChild(header);

      var blurb = document.createElement('div');
      blurb.textContent = def.blurb;
      blurb.style.cssText = 'margin:0 0 14px 0;';
      panel.appendChild(blurb);

      var ladderTitle = document.createElement('div');
      ladderTitle.textContent = 'Rough ladder (easier → tougher)';
      ladderTitle.style.cssText = 'margin-bottom:6px;color:#9aa0a6;font-size:11px;text-transform:uppercase;letter-spacing:0.04em;';
      panel.appendChild(ladderTitle);
      var ladder = document.createElement('div');
      ladder.style.cssText = 'color:#c8c8c8;line-height:1.6;';
      ladder.innerHTML = 'MDN → Cl1 → Cl2 → Cl3 → Cl4 → Cl5 → Cl6 → BM58…BM70+ → OPEN → Listed → G3 → G2 → <b style="color:#f2f2f2;">G1</b>';
      panel.appendChild(ladder);

      var tip = document.createElement('div');
      tip.style.cssText = 'margin-top:14px;color:#9aa0a6;font-size:12px;';
      tip.textContent = 'On picks: ↑ = stepping up vs last start, ↓ = dropping back, → = same band.';
      panel.appendChild(tip);

      document.body.appendChild(backdrop);
      document.body.appendChild(panel);
      return;
    }

    var whyField = (field === 'if_scratched' || field === 'roughie' || field === 'best_pick' || field === 'just_place') ? field : 'best_pick';
    var roleMap = {best_pick: 'Best pick', if_scratched: 'If scratched', just_place: 'Just place', roughie: 'Roughie'};
    var detailRaw = (params.data && params.data[whyField + '_detail']) ? String(params.data[whyField + '_detail']) : '';
    var detail = {};
    try { detail = detailRaw ? JSON.parse(detailRaw) : {}; } catch (e) { detail = {}; }
    var role = detail.role || roleMap[whyField] || 'Best pick';
    var name = detail.name || '';
    var no = detail.no || '';
    var silkUrl = detail.silk || (params.data && params.data[whyField + '_silk']) || '';
    var why = Array.isArray(detail.why) ? detail.why : [];
    var shortWhy = (params.data && params.data.why) ? String(params.data.why) : '';
    if (!name) {
      var disp = (params.data && params.data[whyField]) ? String(params.data[whyField]) : '';
      name = disp.replace(/^[→\s]*/, '').replace(/^\d+\.\s*/, '');
    }

    var backdrop = document.createElement('div');
    backdrop.id = 'roster-why-popup-backdrop';
    backdrop.style.cssText = 'position:fixed;inset:0;z-index:9998;background:rgba(0,0,0,0.4);';
    backdrop.addEventListener('click', closeAllPopups);

    var panel = document.createElement('div');
    panel.id = 'roster-why-popup';
    panel.style.cssText = 'position:fixed;z-index:9999;left:50%;top:10vh;transform:translateX(-50%);width:min(520px,94vw);max-height:80vh;overflow:auto;background:#1e1e1e;color:#f2f2f2;border:1px solid #555;border-radius:12px;padding:16px 18px;box-shadow:0 16px 48px rgba(0,0,0,0.5);font:13px/1.45 -apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;';

    var header = document.createElement('div');
    header.style.cssText = 'display:flex;justify-content:space-between;align-items:flex-start;gap:12px;margin-bottom:12px;';
    var titleWrap = document.createElement('div');
    titleWrap.innerHTML =
      '<div style="font-size:11px;color:#9aa0a6;text-transform:uppercase;letter-spacing:0.04em;">Why this horse?</div>' +
      '<div style="font-size:11px;color:#9aa0a6;margin-top:2px;">' + esc(role) + '</div>' +
      '<div style="font-size:18px;font-weight:650;margin-top:2px;">' + (no ? (esc(no) + '. ') : '') + esc(name || 'Unknown') + '</div>' +
      '<div style="color:#9aa0a6;margin-top:4px;">' + esc(detail.venue || (params.data && params.data.venue) || '') + ' ' +
      esc(detail.race || (params.data && params.data.race) || '') +
      (detail.time || (params.data && params.data.time) ? (' · ' + esc(detail.time || params.data.time)) : '') + '</div>';
    header.appendChild(titleWrap);
    var closeBtn = document.createElement('button');
    closeBtn.textContent = '✕';
    closeBtn.style.cssText = 'background:transparent;border:0;color:#ccc;font-size:18px;cursor:pointer;';
    closeBtn.addEventListener('click', closeAllPopups);
    header.appendChild(closeBtn);
    panel.appendChild(header);

    if (silkUrl) {
      var silkWrap = document.createElement('div');
      silkWrap.style.cssText = 'margin:0 0 12px 0;';
      var img = document.createElement('img');
      img.src = String(silkUrl);
      img.alt = 'Jockey silks';
      img.referrerPolicy = 'no-referrer';
      img.style.height = '110px';
      img.style.width = 'auto';
      silkWrap.appendChild(img);
      panel.appendChild(silkWrap);
    }

    var metaBits = [];
    if (detail.barrier) metaBits.push('Barrier ' + detail.barrier);
    if (detail.jockey) metaBits.push(detail.jockey);
    if (detail.trainer) metaBits.push('T: ' + detail.trainer);
    if (detail.weight) metaBits.push(detail.weight);
    if (detail.last10) metaBits.push('Last10 ' + detail.last10);
    if (metaBits.length) {
      var meta = document.createElement('div');
      meta.style.cssText = 'color:#9aa0a6;margin-bottom:12px;';
      meta.textContent = metaBits.join(' · ');
      panel.appendChild(meta);
    }

    var whyTitle = document.createElement('div');
    whyTitle.textContent = 'Reasons';
    whyTitle.style.cssText = 'margin-bottom:6px;color:#9aa0a6;font-size:11px;text-transform:uppercase;letter-spacing:0.04em;';
    panel.appendChild(whyTitle);

    if (why.length) {
      var ul = document.createElement('ul');
      ul.style.cssText = 'margin:0;padding-left:1.2em;';
      for (var i = 0; i < why.length; i++) {
        var li = document.createElement('li');
        li.style.margin = '4px 0';
        li.textContent = String(why[i]);
        ul.appendChild(li);
      }
      panel.appendChild(ul);
    } else if (shortWhy) {
      var p = document.createElement('div');
      p.textContent = shortWhy;
      panel.appendChild(p);
    } else {
      var none = document.createElement('div');
      none.style.color = '#9aa0a6';
      none.textContent = 'No why rationale available for this horse yet.';
      panel.appendChild(none);
    }

    if (detail.profile_url) {
      var link = document.createElement('a');
      link.href = detail.profile_url;
      link.target = '_blank';
      link.rel = 'noopener';
      link.textContent = 'Open horse form';
      link.style.cssText = 'display:inline-block;margin-top:14px;color:#8ab4f8;';
      panel.appendChild(link);
    }

    document.body.appendChild(backdrop);
    document.body.appendChild(panel);
    return;
  }

  // Shift+click → full field grid
  var raw = (params.data && params.data.field_json) ? String(params.data.field_json) : '';
  var payload = {};
  try { payload = raw ? JSON.parse(raw) : {}; } catch (e) { payload = {}; }
  var runners = Array.isArray(payload.runners) ? payload.runners : [];
  var venue = payload.venue || (params.data && params.data.venue) || '';
  var race = payload.race || (params.data && params.data.race) || '';
  var time = payload.time || (params.data && params.data.time) || '';
  var distance = payload.distance || (params.data && params.data['race length']) || '';
  var track = payload.track || (params.data && params.data.track) || '';

  var backdrop = document.createElement('div');
  backdrop.id = 'roster-field-popup-backdrop';
  backdrop.style.cssText = 'position:fixed;inset:0;z-index:9998;background:rgba(0,0,0,0.4);';
  backdrop.addEventListener('click', closeAllPopups);

  var panel = document.createElement('div');
  panel.id = 'roster-field-popup';
  panel.style.cssText = 'position:fixed;z-index:9999;left:50%;top:6vh;transform:translateX(-50%);width:min(960px,94vw);max-height:88vh;overflow:auto;background:#1e1e1e;color:#f2f2f2;border:1px solid #555;border-radius:12px;padding:16px 18px;box-shadow:0 16px 48px rgba(0,0,0,0.5);font:13px/1.4 -apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;';

  var header = document.createElement('div');
  header.style.cssText = 'display:flex;justify-content:space-between;align-items:flex-start;gap:12px;margin-bottom:12px;';
  var titleWrap = document.createElement('div');
  titleWrap.innerHTML = '<div style="font-size:11px;color:#9aa0a6;text-transform:uppercase;letter-spacing:0.04em;">Full field</div>' +
    '<div style="font-size:18px;font-weight:650;">' + esc(venue) + ' ' + esc(race) + '</div>' +
    '<div style="color:#9aa0a6;margin-top:2px;">' + esc(time) + (distance ? (' · ' + esc(distance)) : '') +
    (payload.class_label ? (' · ' + esc(payload.class_label)) : '') +
    (track ? (' · ' + esc(track)) : '') +
    ' · ' + runners.length + ' runners</div>';
  header.appendChild(titleWrap);
  var closeBtn = document.createElement('button');
  closeBtn.textContent = '✕';
  closeBtn.style.cssText = 'background:transparent;border:0;color:#ccc;font-size:18px;cursor:pointer;';
  closeBtn.addEventListener('click', closeAllPopups);
  header.appendChild(closeBtn);
  panel.appendChild(header);

  if (!runners.length) {
    var empty = document.createElement('div');
    empty.textContent = 'No field loaded for this race.';
    empty.style.color = '#9aa0a6';
    panel.appendChild(empty);
  } else {
    var table = document.createElement('table');
    table.style.cssText = 'width:100%;border-collapse:collapse;';
    var thead = document.createElement('thead');
    thead.innerHTML = '<tr>' +
      ['','No','Horse','Class','Barrier','Jockey','Trainer','Wt','Rating','Last 10','Tag'].map(function(h) {
        return '<th style="text-align:left;padding:8px 6px;border-bottom:1px solid #444;color:#9aa0a6;font-size:11px;text-transform:uppercase;position:sticky;top:0;background:#1e1e1e;">' + h + '</th>';
      }).join('') + '</tr>';
    table.appendChild(thead);
    var tbody = document.createElement('tbody');
    for (var i = 0; i < runners.length; i++) {
      var r = runners[i] || {};
      var tr = document.createElement('tr');
      tr.style.background = (i % 2 === 0) ? 'rgba(255,255,255,0.03)' : 'rgba(255,255,255,0.08)';
      if (r.scratched) tr.style.opacity = '0.55';
      var silkCell = document.createElement('td');
      silkCell.style.cssText = 'padding:6px;border-bottom:1px solid #333;width:40px;';
      if (r.silk) {
        var img = document.createElement('img');
        img.src = String(r.silk);
        img.alt = '';
        img.referrerPolicy = 'no-referrer';
        img.style.height = '28px';
        img.style.width = 'auto';
        silkCell.appendChild(img);
      }
      tr.appendChild(silkCell);
      function td(v, extra) {
        var cell = document.createElement('td');
        cell.style.cssText = 'padding:6px;border-bottom:1px solid #333;vertical-align:middle;' + (extra || '');
        cell.textContent = (v === undefined || v === null) ? '' : String(v);
        return cell;
      }
      tr.appendChild(td(r.no, 'width:36px;'));
      tr.appendChild(td(r.name + (r.scratched ? ' (SCR)' : ''), 'font-weight:600;'));
      var classTd = document.createElement('td');
      classTd.style.cssText = 'padding:6px;border-bottom:1px solid #333;vertical-align:middle;white-space:nowrap;';
      var arrow = r.class_arrow || '';
      var lastCls = r.last_class || '';
      var arrowColor = arrow === '↑' ? '#f0a0a0' : (arrow === '↓' ? '#8fd19e' : '#c8c8c8');
      classTd.innerHTML = (arrow ? ('<span style="color:' + arrowColor + ';font-weight:700;">' + arrow + '</span> ') : '') +
        (lastCls ? ('<span style="color:#9aa0a6;">' + esc(lastCls) + '</span>') : '');
      tr.appendChild(classTd);
      tr.appendChild(td(r.barrier));
      tr.appendChild(td(r.jockey));
      tr.appendChild(td(r.trainer));
      tr.appendChild(td(r.weight));
      tr.appendChild(td(r.benchmark));
      tr.appendChild(td(r.last10));
      var mark = r.mark || '';
      var markTd = td(mark ? ('★ ' + mark) : '');
      if (mark === 'Pick') markTd.style.color = '#8ab4f8';
      else if (mark === 'Place') markTd.style.color = '#78dce8';
      else if (mark === 'Backup') markTd.style.color = '#81c995';
      else if (mark === 'Roughie') markTd.style.color = '#fdd663';
      tr.appendChild(markTd);
      tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    panel.appendChild(table);
  }

  document.body.appendChild(backdrop);
  document.body.appendChild(panel);
}
"""
                )
                if JsCode is not None
                else None
            )
            def _is_current_js_check() -> str:
                return (
                    "(params.data.is_current === 1 || params.data.is_current === true || "
                    "params.data.is_current === '1' || params.data.is_current === 'True')"
                )

            get_row_style = (
                JsCode(
                    rf"""
function(params) {{
  if (!params.data) return null;
  var cur = {_is_current_js_check()};
  if (cur) {{
    return {{
      background: 'rgba(212, 160, 23, 0.42)',
      boxShadow: 'inset 4px 0 0 #d4a017',
      fontWeight: '650'
    }};
  }}
  if (params.node && params.node.rowIndex % 2 === 1) {{
    return {{ background: 'rgba(255,255,255,0.10)' }};
  }}
  return null;
}}
"""
                )
                if JsCode is not None
                else None
            )
            # Client-side ticker: recompute amber "current/next" row from browser clock
            # so the highlight advances without a Streamlit reload.
            current_race_ticker = (
                JsCode(
                    r"""
function(params) {
  var api = params.api;
  if (!api) return;

  function liveWindowMs(code) {
    var c = String(code || '').toLowerCase();
    if (c === 'greyhound') return 5 * 60 * 1000;
    if (c === 'harness') return 8 * 60 * 1000;
    return 8 * 60 * 1000;
  }

  function parseDtMs(v) {
    if (v === undefined || v === null || v === '') return null;
    var n = Number(v);
    return isFinite(n) ? n : null;
  }

  function isCur(v) {
    return v === 1 || v === true || v === '1' || v === 'True';
  }

  function refreshCurrent(scrollAlways) {
    var now = Date.now();
    var bestLive = null, bestLiveDt = -1;
    var bestUp = null, bestUpDt = Infinity;
    var bestFin = null, bestFinDt = -1;

    api.forEachNode(function(node) {
      if (!node || !node.data) return;
      var dt = parseDtMs(node.data.dt_ms);
      if (dt == null) return;
      var win = liveWindowMs(node.data._code);
      if (dt <= now && now <= dt + win) {
        if (dt >= bestLiveDt) { bestLiveDt = dt; bestLive = node; }
      } else if (dt > now) {
        if (dt < bestUpDt) { bestUpDt = dt; bestUp = node; }
      } else if (dt > bestFinDt) {
        bestFinDt = dt; bestFin = node;
      }
    });

    var target = bestLive || bestUp || bestFin;
    var changed = [];
    var prevIdx = -1;
    var nextIdx = target ? target.rowIndex : -1;

    api.forEachNode(function(node) {
      if (!node || !node.data) return;
      var want = !!(target && node === target);
      var had = isCur(node.data.is_current);
      if (had) prevIdx = node.rowIndex;
      if (want !== had) {
        node.data.is_current = want ? 1 : 0;
        changed.push(node);
      }
    });

    if (changed.length) {
      try { api.redrawRows({ rowNodes: changed }); } catch (e) {
        try { api.redrawRows(); } catch (e2) {}
      }
    }
    // Scroll on first paint, or when the highlighted race changes.
    if (scrollAlways && nextIdx >= 0) {
      try { api.ensureIndexVisible(nextIdx, 'middle'); } catch (e) {}
    } else if (nextIdx >= 0 && nextIdx !== prevIdx) {
      try { api.ensureIndexVisible(nextIdx, 'middle'); } catch (e) {}
    }
  }

  refreshCurrent(true);
  try {
    if (window.__rosterCurrentTimer) clearInterval(window.__rosterCurrentTimer);
  } catch (e) {}
  window.__rosterCurrentTimer = setInterval(function() { refreshCurrent(false); }, 15000);
}
"""
                )
                if JsCode is not None
                else None
            )
            grid_opts_kwargs = dict(
                suppressRowClickSelection=True,
                preventDefaultOnContextMenu=True,
                rowClassRules={
                    "roster-ag-row-current": "data.is_current == 1 || data.is_current === true || data.is_current === '1' || data.is_current === 'True'",
                },
            )
            if get_row_style is not None:
                grid_opts_kwargs["getRowStyle"] = get_row_style
            if row_modifier_click is not None:
                grid_opts_kwargs["onCellClicked"] = row_modifier_click
            if current_race_ticker is not None:
                grid_opts_kwargs["onFirstDataRendered"] = current_race_ticker
            gb.configure_grid_options(**grid_opts_kwargs)
            grid_options = gb.build()
            aggrid_kwargs = dict(
                data=df,
                gridOptions=grid_options,
                theme="streamlit",
                height=840,
                fit_columns_on_grid_load=False,
                update_mode=GridUpdateMode.NO_UPDATE,
                update_on=["cellContextMenu"] if should_return_race_ctx is not None else [],
                allow_unsafe_jscode=True,
                # Styles must be injected into the AgGrid iframe (st.markdown CSS does not reach it).
                # Theme paints .ag-cell backgrounds, so highlight cells as well as the row.
                custom_css={
                    ".ag-row.roster-ag-row-current": {
                        "background-color": "rgba(212, 160, 23, 0.45) !important",
                        "border-left": "4px solid #d4a017",
                    },
                    ".ag-row.roster-ag-row-current .ag-cell": {
                        "background-color": "rgba(212, 160, 23, 0.45) !important",
                        "font-weight": "650",
                    },
                    ".ag-row.roster-ag-row-current:hover .ag-cell": {
                        "background-color": "rgba(212, 160, 23, 0.58) !important",
                    },
                    ".ag-cell.roster-fs-ntd, .ag-row.roster-ag-row-current .ag-cell.roster-fs-ntd": {
                        "background-color": "rgba(230, 140, 40, 0.85) !important",
                        "color": "#1a1a1a !important",
                        "font-weight": "700 !important",
                    },
                    ".ag-cell.roster-fs-noplace, .ag-row.roster-ag-row-current .ag-cell.roster-fs-noplace": {
                        "background-color": "rgba(200, 60, 50, 0.85) !important",
                        "color": "#ffffff !important",
                        "font-weight": "700 !important",
                    },
                },
                key="roster_aggrid_silks_v20",
            )
            if should_return_race_ctx is not None and collect_race_ctx is not None:
                aggrid_kwargs["data_return_mode"] = DataReturnMode.CUSTOM
                aggrid_kwargs["should_grid_return"] = should_return_race_ctx
                aggrid_kwargs["custom_jscode_for_grid_return"] = collect_race_ctx
            grid_return = AgGrid(**aggrid_kwargs)
            payload = getattr(grid_return, "raw_data", None)
            if isinstance(payload, dict) and payload.get("action") == "race_results":
                nonce = payload.get("nonce")
                if nonce is not None and nonce != st.session_state.get("_roster_race_result_nonce"):
                    st.session_state["_roster_race_result_nonce"] = nonce
                    meeting_url = str(payload.get("meeting_link") or "").strip()
                    race_no_raw = payload.get("race_no")
                    try:
                        race_no_int = int(race_no_raw) if race_no_raw is not None else None
                    except (TypeError, ValueError):
                        race_no_int = None
                    if meeting_url and race_no_int is not None:
                        race_result_dialog(
                            chosen_date=chosen_date,
                            meeting_url=meeting_url,
                            code=str(payload.get("_code") or "thoroughbred"),
                            race_no=race_no_int,
                            venue=str(payload.get("venue") or ""),
                            race_label=str(payload.get("race") or ""),
                        )
            if current_race_label:
                st.caption(f"Highlighted current/next race: **{current_race_label}**")
            with st.expander("Grid shortcuts (click & keyboard)", expanded=True):
                st.markdown(
                    """
| Action | What it does |
| --- | --- |
| **Normal click** | Does nothing (page does not reload) |
| **Shift + click** (any cell on a row) | Opens the **full field** for that race |
| **Best pick / If scratched / Just place / Roughie** | Program no + name + **(barrier)** + **$odds fluc** (Sportsbet) + class ↑/↓ |
| **Odds fluc** | **↓** shortening · **↑** drifting · **→** steady (from recent price history) |
| **Jump alerts** (top banner) | Live countdown — races in next **20 min** (updates every 5s) |
| **Scratchings board** | Field SCR + Sportsbet late outs; warns when a tip is scratched |
| **Ctrl + click** / **⌘ + click** (pick / other cells) | Opens **Why this horse?** (best pick by default; pick columns use that pick) |
| **Ctrl + click** / **⌘ + click** on **Class** | Explains that race grade (**MDN**, **Cl1–6**, **BM**, **G1–3**, …) |
| **Right-click Best pick / If scratched / Just place / Roughie** | Pick detail panel + **5× silk** (toggle to shrink) |
| **Class** | Race grade from the title: **MDN**, **Cl1–6**, **BM58**, **G1–3**, **OPEN**, etc. |
| **Track** | Meeting track condition from the card (**Soft5**, **Good4**, **Heavy9**, …) |
| **Class ↑ / ↓ / →** | On picks & full field: vs horse’s last race start (**↑** up, **↓** down, **→** same). From Form.aspx. |
| **Just place** | Place-market tip: rank 2 when score gap is clear (≥0.05), else rank 1 |
| **If scratched** | Backup if the win tip is scratched (rank 2) |
| **Roughie** | Long-shot (last ranked) |
| **Right-click Race** | Load **results** for that race (if posted) |
| **Amber highlighted row** | Current / next race — updates every **15s** from your clock (no reload) |
| **Field size orange (NTD)** | **5–7** starters — no 3rd place dividend (place pays 1st & 2nd only) |
| **Field size red (no place)** | **Fewer than 5** starters — place market usually not offered |
| **Drag column edges** | Resize columns |
"""
                )
            st.caption("Tip: pick columns show silks; Race column right-click fetches results when available.")
        else:
            st.dataframe(
                display_rows,
                width="stretch",
                hide_index=True,
                column_order=roster_cols,
                column_config=roster_column_config,
            )
            st.caption("Tip: drag column edges to resize. Switch to 'Interactive rows' for an inline WHY button per row.")
        return

    if mode.startswith("Pretty"):
        # Pretty HTML-only rendering: full-row zebra + aligned columns, but no Streamlit popovers.
        st.caption("Pretty view uses HTML expanders (not Streamlit popovers). Drag column right edge to resize. Switch to Interactive for real WHY popups.")
        st.markdown(
            """
<style>
  .roster-row { display: flex; align-items: center; flex-wrap: nowrap; gap: 6px; margin: 2px 0; border-radius: 6px; padding: 7px 6px; min-height: 2.2em; box-sizing: border-box; }
  .roster-cell { flex: 0 0 auto; overflow-x: auto; overflow-y: hidden; text-overflow: ellipsis; white-space: nowrap; resize: horizontal; min-width: 2.5rem; }
  .roster-cell a { text-decoration: underline; }
  .roster-cell details { font-size: 0.9em; }
  .roster-cell details summary { cursor: pointer; list-style: none; }
  .roster-cell details summary::-webkit-details-marker { display: none; }
  .roster-cell:has(details[open]) { overflow: visible; white-space: normal; resize: none; flex: 0 0 auto; min-width: 14em; }
  .roster-cell details[open] { display: block; max-height: 18em; overflow-y: auto; overflow-x: hidden; }
  .roster-cell details[open] ul { display: block; white-space: normal; min-width: 11em; margin: 0; padding-left: 1.4em; list-style: disc; line-height: 1.5; }
  .roster-field-ul { margin: 4px 0; padding-left: 1.4em; font-size: 0.9em; min-width: 11em; list-style: disc; line-height: 1.5; display: block; }
  .roster-field-li { display: block; white-space: normal; word-break: break-word; margin: 2px 0; min-height: 1.4em; }
  .roster-cell-pick { white-space: normal !important; overflow-wrap: break-word; min-width: 7em; text-overflow: clip; }
  .roster-header { background: transparent !important; font-weight: 600; margin-bottom: 4px; }
</style>
""",
            unsafe_allow_html=True,
        )

        # Sky columns no longer shown in Pretty view
        col_widths = [0.028, 0.08, 0.05, 0.05, 0.05, 0.06, 0.04, 0.05, 0.12, 0.12, 0.10, 0.09, 0.06, 0.04, 0.04]
        hdr_labels = ["", "**venue**", "**type**", "**race**", "**time**", "**length**", "**field**", "**runners**", "**best pick**", "**if scratched**", "**roughie**", "**why (short)**", "**Open**", "**odds**", "**why**"]
        col_pct = [f"{w * 100:.1f}%" for w in col_widths]
        n_cols = len(col_widths)

        def cell_html(content: str, flex: str, allow_wrap: bool = False) -> str:
            cls = "roster-cell roster-cell-pick" if allow_wrap else "roster-cell"
            return f'<div class="{cls}" style="width: {flex}; min-width: 2.5rem;">{content}</div>'

        hdr_row = '<div class="roster-row roster-header">' + "".join(cell_html(hdr_labels[i], col_pct[i]) for i in range(n_cols)) + "</div>"
        st.markdown(hdr_row, unsafe_allow_html=True)

        for row_i, r in enumerate(rows_sorted):
            bar_bg = "rgba(255,255,255,0.12)" if (row_i % 2 == 0) else "rgba(255,255,255,0.04)"
            race_link = str(r.get("race_link") or "")
            pick = r.get("best_pick", "")
            pick_no = r.get("best_pick_no", "")
            _runners_pretty = _runners_objects_for_roster_row(r)
            _cls_pretty = str(r.get("class") or "")
            pick_disp = _format_pick_cell(
                name=pick, no=pick_no, runners=_runners_pretty, today_class=_cls_pretty
            )
            bkup = r.get("if_scratched", "")
            bkup_no = r.get("if_scratched_no", "")
            bkup_disp = _format_pick_cell(
                name=bkup, no=bkup_no, runners=_runners_pretty, today_class=_cls_pretty, prefix="→ "
            )

            fs_val = r.get("field_size")
            fs_str = str(fs_val) if fs_val is not None else ""
            c0 = cell_html("", col_pct[0])
            c1 = cell_html(html.escape(r.get("venue", "")), col_pct[1])
            c_type = cell_html(html.escape(r.get("type", "")), col_pct[2])
            c2 = cell_html(html.escape(r.get("race", "")), col_pct[3])
            c3 = cell_html(html.escape(r.get("time", "")), col_pct[4])
            c4 = cell_html(html.escape(r.get("race_length", "—")), col_pct[5])
            c5_field = cell_html(html.escape(fs_str), col_pct[6])
            field_list = _runners_for_roster_row(r)
            best_name = (r.get("best_pick") or "").strip()
            backup_name = (r.get("if_scratched") or "").strip()
            runners_items = []
            for num, name, scratched, silk in field_list:
                suffix = " (SCR)" if scratched else ""
                if name == best_name:
                    suffix = " ★\u00a0Pick" + suffix  # nbsp so "Pick" stays with ★
                elif name == backup_name:
                    suffix = " ★\u00a0Backup" + suffix
                silk_html = (
                    f'<img src="{html.escape(silk)}" height="20" style="vertical-align:middle;margin-right:4px;" referrerpolicy="no-referrer" />'
                    if silk
                    else ""
                )
                runners_items.append(
                    f"<li class='roster-field-li'>{html.escape(num)}. {silk_html}{html.escape(name)}{html.escape(suffix)}</li>"
                )
            runners_details = (
                f"<details><summary>Field ▾</summary><ul class='roster-field-ul'>{''.join(runners_items)}</ul></details>"
                if field_list else "<span style='font-size:0.85em;'>—</span>"
            )
            c_runners = cell_html(runners_details, col_pct[7])
            c6 = cell_html(html.escape(pick_disp), col_pct[8], allow_wrap=True)
            c7_bkup = cell_html(html.escape(bkup_disp), col_pct[9], allow_wrap=True)
            rough_disp = _format_pick_cell(
                name=r.get("roughie", "") or "",
                no=r.get("roughie_no", "") or "",
                runners=_runners_pretty,
                today_class=_cls_pretty,
            )
            c_roughie = cell_html(html.escape(rough_disp), col_pct[10], allow_wrap=True)
            c7 = cell_html("", col_pct[11])
            open_a = f'<a href="{html.escape(race_link)}" target="_blank" rel="noopener">Open</a>' if race_link else "Open"
            c8 = cell_html(open_a, col_pct[12])

            q = f"site:tab.com.au racing {chosen_date.isoformat()} {r.get('venue','')} {r.get('race','')}"
            search_url = f"https://www.google.com/search?q={quote(q)}"
            odds_details = (
                "<details><summary>Odds ▾</summary>"
                "<p style='margin:4px 0;font-size:0.85em;'>No live odds in app.</p>"
                '<a href="https://www.tab.com.au/racing" target="_blank" rel="noopener">Open TAB Racing</a><br>'
                f'<a href="{search_url}" target="_blank" rel="noopener">Search TAB for this race</a></details>'
            )
            c9 = cell_html(odds_details, col_pct[13])

            bullets = r.get("_best_pick_why") or []
            backup_bullets = r.get("_backup_pick_why") or []
            why_inner = (
                f"<p><strong>{html.escape(r.get('venue',''))} {html.escape(r.get('race',''))}</strong></p>"
                f"<p><strong>Pick:</strong> {html.escape(pick_disp)}</p>"
            )
            if r.get("if_scratched"):
                why_inner += f"<p><em>If scratched: {html.escape(bkup_disp)}</em></p>"
            if bullets:
                why_inner += "<ul style='margin:4px 0;padding-left:1.2em;'>" + "".join(
                    f"<li>{html.escape(b)}</li>" for b in bullets[:12]
                ) + "</ul>"
            else:
                why_inner += "<p>No rationale (v0).</p>"
            if backup_bullets:
                why_inner += "<p><strong>Backup:</strong></p><ul style='margin:4px 0;padding-left:1.2em;'>" + "".join(
                    f"<li>{html.escape(b)}</li>" for b in backup_bullets[:8]
                ) + "</ul>"
            if race_link:
                why_inner += f'<p><a href="{html.escape(race_link)}" target="_blank" rel="noopener">Open race page</a></p>'
            why_details = f"<details><summary>WHY ▾</summary><div style='max-height:12em;overflow:auto;'>{why_inner}</div></details>"
            c10 = cell_html(why_details, col_pct[14])

            row_html = (
                f'<div class="roster-row" style="background:{bar_bg};">'
                + f"{c0}{c1}{c_type}{c2}{c3}{c4}{c5_field}{c_runners}{c6}{c7_bkup}{c_roughie}{c7}{c8}{c9}{c10}"
                + "</div>"
            )
            st.markdown(row_html, unsafe_allow_html=True)
        return

    if len(rows_sorted) > 300:
        st.warning("Too many rows for interactive mode; switch to Table or enable filters.")
        return

    # Interactive row rendering (Streamlit widgets) so WHY/Odds are true popovers again.
    st.caption("Each row has its own WHY + open links (no giant URLs).")
    has_any_pick = any(r.get("best_pick") for r in rows_sorted)
    if not has_any_pick:
        st.caption("No picks in table: runner fetch may be blocked (e.g. 403) or limit reached. Picks are computed automatically (limit 50).")

    st.markdown(
        """
<style>
  /* Keep action buttons readable (avoid per-letter wrapping). */
  div[data-testid="stDialog"] button,
  div[data-testid="stDialog"] a[role="button"]{
    white-space: nowrap !important;
  }
  .roster-pick-cell { word-break: break-word; overflow-wrap: break-word; line-height: 1.35; min-width: 0; }
</style>
""",
        unsafe_allow_html=True,
    )

    # Give best pick / if scratched / roughie width so names don't truncate
    col_widths = [0.028, 0.08, 0.05, 0.05, 0.05, 0.06, 0.04, 0.04, 0.12, 0.12, 0.10, 0.09, 0.06, 0.04, 0.04]

    hdr = st.columns(col_widths, vertical_alignment="center")
    hdr[0].markdown("")
    hdr[1].markdown("**venue**")
    hdr[2].markdown("**type**")
    hdr[3].markdown("**race**")
    hdr[4].markdown("**time**")
    hdr[5].markdown("**length**")
    hdr[6].markdown("**field**")
    hdr[7].markdown("**runners**")
    hdr[8].markdown("**best pick**")
    hdr[9].markdown("**if scratched**")
    hdr[10].markdown("**roughie**")
    hdr[11].markdown("**why (short)**")
    hdr[12].markdown("**Open**")
    hdr[13].markdown("**odds**")
    hdr[14].markdown("**why**")

    for row_i, r in enumerate(rows_sorted):
        rid = hashlib.sha1(f"{r.get('meeting_link','')}|{r.get('race','')}".encode("utf-8")).hexdigest()[:10]
        cols = st.columns(col_widths, vertical_alignment="center")

        # A small stripe in the first column for readability (full-row background tends to break alignment).
        bar_bg = "rgba(255,255,255,0.12)" if (row_i % 2 == 0) else "rgba(255,255,255,0.04)"
        cols[0].markdown(
            f"<div style='background:{bar_bg}; margin:-6px 0; padding:10px 4px; min-height:1.6em; border-radius:3px;'></div>",
            unsafe_allow_html=True,
        )

        cols[1].write(r.get("venue", ""))
        cols[2].write(r.get("type", ""))
        cols[3].write(r.get("race", ""))
        cols[4].write(r.get("time", ""))
        cols[5].write(r.get("race_length", "—"))
        fs = r.get("field_size")
        cols[6].write(str(fs) if fs is not None else "")

        with cols[7]:
            field_list = _runners_for_roster_row(r)
            best_name = (r.get("best_pick") or "").strip()
            backup_name = (r.get("if_scratched") or "").strip()
            with st.popover("Field", help="Show all runners with box/draw numbers; ★ marks our picks"):
                st.caption(f"**{r.get('venue','')} {r.get('race','')}** — {len(field_list)} runner(s)")
                if field_list:
                    for num, name, scratched, silk in field_list:
                        suffix = " (SCR)" if scratched else ""
                        if name == best_name:
                            suffix = " ★ Pick" + suffix
                        elif name == backup_name:
                            suffix = " ★ Backup" + suffix
                        if silk:
                            st.markdown(
                                f'{html.escape(str(num))}. '
                                f'<img src="{html.escape(silk)}" height="26" style="vertical-align:middle;margin-right:6px;" referrerpolicy="no-referrer" />'
                                f'<b>{html.escape(name)}</b>{html.escape(suffix)}',
                                unsafe_allow_html=True,
                            )
                        else:
                            st.markdown(f"{num}. **{name}**{suffix}")
                else:
                    st.caption("Field not loaded (use main page to load this meeting).")

        pick = r.get("best_pick", "")
        pick_no = r.get("best_pick_no", "")
        _runners_int = _runners_objects_for_roster_row(r)
        _cls_int = str(r.get("class") or "")
        pick_text = _format_pick_cell(
            name=pick, no=pick_no, runners=_runners_int, today_class=_cls_int
        )
        cols[8].markdown(f'<div class="roster-pick-cell">{html.escape(pick_text)}</div>', unsafe_allow_html=True)

        bkup = r.get("if_scratched", "")
        bkup_no = r.get("if_scratched_no", "")
        bkup_text = _format_pick_cell(
            name=bkup, no=bkup_no, runners=_runners_int, today_class=_cls_int, prefix="→ "
        )
        cols[9].markdown(f'<div class="roster-pick-cell">{html.escape(bkup_text)}</div>', unsafe_allow_html=True)

        rough = r.get("roughie", "")
        rough_no = r.get("roughie_no", "")
        rough_text = _format_pick_cell(
            name=rough, no=rough_no, runners=_runners_int, today_class=_cls_int
        )
        cols[10].markdown(f'<div class="roster-pick-cell">{html.escape(rough_text)}</div>', unsafe_allow_html=True)

        cols[11].write("")

        with cols[12]:
            st.link_button(
                "Open",
                url=str(r.get("race_link") or ""),
                type="secondary",
                disabled=not bool(r.get("race_link")),
            )
            rno = r.get("race_no")
            if rno is not None:
                rno_int = int(rno) if not isinstance(rno, int) else rno
                if st.button("Results", key=f"res_ir_{rid}"):
                    race_result_dialog(
                        chosen_date=chosen_date,
                        meeting_url=str(r.get("meeting_link") or ""),
                        code=str(r.get("_code") or "thoroughbred"),
                        race_no=rno_int,
                        venue=str(r.get("venue") or ""),
                        race_label=str(r.get("race") or ""),
                    )

        with cols[13]:
            with st.popover("Odds", help="Open public TAB odds page (manual)"):
                st.caption("This app does not fetch live odds automatically (no API key).")
                st.link_button("Open TAB Racing", url="https://www.tab.com.au/racing", type="secondary")
                q = f"site:tab.com.au racing {chosen_date.isoformat()} {r.get('venue','')} {r.get('race','')}"
                st.link_button("Search TAB for this race", url=f"https://www.google.com/search?q={quote(q)}", type="secondary")

        with cols[14]:
            disabled = not bool(r.get("best_pick") and (r.get("_best_pick_why") or []))
            with st.popover("WHY", disabled=disabled, help="Show rationale for the best pick"):
                st.write(f"**{r.get('venue','')} {r.get('race','')}**")
                pick = r.get("best_pick", "")
                pick_no = r.get("best_pick_no", "")
                st.write(f"**Pick:** {(f'{pick_no}. {pick}' if pick_no and pick else pick)}")
                if r.get("if_scratched"):
                    bkup = r.get("if_scratched", "")
                    bkup_no = r.get("if_scratched_no", "")
                    st.caption(f"If scratched: **{(f'{bkup_no}. {bkup}' if bkup_no and bkup else bkup)}**")
                bullets = r.get("_best_pick_why") or []
                if bullets:
                    for b in bullets[:12]:
                        st.write(f"- {b}")
                else:
                    st.info("No rationale available (v0).")
                if r.get("_backup_pick_why"):
                    with st.expander(f"Backup rationale (if scratched: {r.get('if_scratched')})", expanded=False):
                        for b in (r.get("_backup_pick_why") or [])[:8]:
                            st.write(f"- {b}")
                if r.get("race_link"):
                    st.link_button("Open race page", url=str(r.get("race_link")), type="secondary")


@st.dialog("Race result")
def race_result_dialog(
    *,
    chosen_date: date,
    meeting_url: str,
    code: str,
    race_no: int,
    venue: str,
    race_label: str,
) -> None:
    """Show winner and placings for a single race (best-effort from result pages)."""
    st.write(f"**{venue}** {race_label}")
    st.caption(f"Date: {chosen_date.isoformat()} · Results are fetched from public result pages (best-effort).")
    results: dict = {}
    with st.spinner("Fetching result..."):
        try:
            results = fetch_results_for_meeting(code, meeting_url) or {}
        except Exception as e:
            st.warning(f"Couldn’t fetch results: {e}")
            return
    if results:
        db_persist_results(chosen_date, meeting_url, code, results)
    res = results.get(race_no) if results else None
    if not res:
        st.info("No result for this race yet. Results may not be posted or parsing didn’t match (v0).")
        return
    st.markdown(f"**Winner:** {res.winner or '—'}")
    if getattr(res, "places", None) and len(res.places) >= 2:
        st.markdown(f"**2nd:** {res.places[1]}")
    if getattr(res, "places", None) and len(res.places) >= 3:
        st.markdown(f"**3rd:** {res.places[2]}")
    if res.source_url:
        st.link_button("Open result page", url=res.source_url, type="secondary")


@st.dialog("Race roster (what's run / what's next)")
def race_roster_dialog(*, chosen_date: date, code_label: str, meetings: list, fields_by_meeting: dict, open_nonce: int = 0) -> None:
    render_roster_content(chosen_date=chosen_date, code_label=code_label, meetings=meetings, fields_by_meeting=fields_by_meeting, open_nonce=open_nonce)


@st.dialog("Daily review (winners vs our picks)")
def daily_review_dialog(chosen_date: date) -> None:
    st.write(f"**Date:** {chosen_date.isoformat()}")

    # --- Jockey / driver leaderboard (from stored fields + results) ---
    with st.expander("Jockey / driver leaderboard", expanded=True):
        scope = st.radio(
            "Scope",
            options=["This date", "Last 7 days", "All time"],
            horizontal=True,
            key="jockey_stats_scope",
        )
        code_j = st.selectbox(
            "Code",
            options=["thoroughbred", "harness", "greyhound"],
            index=0,
            key="jockey_stats_code",
            format_func=lambda c: {"thoroughbred": "Thoroughbred (jockeys)", "harness": "Harness (drivers)", "greyhound": "Greyhounds"}.get(c, c),
        )
        min_rides = st.slider("Min rides", 1, 20, 3, key="jockey_stats_min")
        date_from = None
        date_to = None
        if scope == "This date":
            date_from = chosen_date
            date_to = chosen_date
        elif scope == "Last 7 days":
            date_from = chosen_date - timedelta(days=6)
            date_to = chosen_date
        stats = jockey_stats(
            code=code_j,
            date_from=date_from,
            date_to=date_to,
            min_rides=int(min_rides),
            limit=40,
        )
        if not stats:
            st.caption(
                "No jockey rides yet for this filter. Fetch results (right-click Race, or review below) "
                "while fields are loaded — rides sync automatically. Or click **Backfill jockey rides** in Controls."
            )
        else:
            st.caption(
                "↑ sorted by place% · **pick_*** = when they rode our best tip · "
                "Names collapse apprentice claims (a)."
            )
            st.dataframe(stats, width="stretch", hide_index=True)

    picks = load_picks(chosen_date)
    if not picks:
        st.info(
            "No saved picks for this date yet. Open the race roster so picks can auto-save, "
            "or rank a race and click 'Save pick to Daily review'."
        )
        return

    code_label = st.selectbox(
        "Code filter",
        options=["All", "Greyhounds", "Thoroughbred", "Harness (NSW)"],
        index=0,
    )
    show_all_results = st.toggle("Show all available results for the day (slow)", value=False)

    def label_to_code(lbl: str) -> str:
        if lbl == "Greyhounds":
            return "greyhound"
        if lbl == "Thoroughbred":
            return "thoroughbred"
        if lbl == "Harness (NSW)":
            return "harness"
        return ""

    wanted_codes = ["greyhound", "thoroughbred", "harness"] if code_label == "All" else [label_to_code(code_label)]
    picks = [p for p in picks if p.get("code") in wanted_codes]
    if not picks:
        st.info("No picks for this code/date combination.")
        if not show_all_results:
            return

    st.caption(
        "Winners are fetched best-effort from public result pages; missing winners usually means results aren’t posted yet or parsing didn’t match (v0)."
    )

    # Index picks by meeting/race for quick lookup
    picks_by_meeting_race: dict[tuple[str, int], dict] = {}
    for p in picks:
        try:
            picks_by_meeting_race[(p.get("meeting_url", ""), int(p.get("race_no") or 0))] = p
        except Exception:
            continue

    def render_meeting(*, meeting_url: str, venue: str, code: str, meeting_date: str) -> None:
        # Load results from DB first; if missing, fetch and persist
        stored_results = db_load_results(chosen_date, meeting_url, code)
        if not stored_results:
            with st.spinner(f"Fetching winners for {venue} ({code})..."):
                results = fetch_results_for_meeting(code, meeting_url)
            db_persist_results(chosen_date, meeting_url, code, results or {})
            stored_results = {}
            for rn, res in (results or {}).items():
                places = getattr(res, "places", ()) or ()
                stored_results[rn] = {
                    "winner": (res.winner if res else None) or "",
                    "place2": places[1] if len(places) > 1 else "",
                    "place3": places[2] if len(places) > 2 else "",
                }

        race_nos = sorted({*stored_results.keys(), *[rn for (mu, rn) in picks_by_meeting_race.keys() if mu == meeting_url and rn]})
        if not race_nos:
            return

        saved = len([1 for (mu, rn) in picks_by_meeting_race.keys() if mu == meeting_url and rn])
        with st.expander(f"{venue} — {code} — {meeting_date} ({saved} saved picks)", expanded=False):
            for rn in race_nos:
                p = picks_by_meeting_race.get((meeting_url, rn))
                res_row = stored_results.get(rn) or {}
                winner = (res_row.get("winner") or "N/A").strip() or "N/A"
                pick_name = (p.get("pick_name") if p else None) or "—"
                hit = (winner != "N/A") and (pick_name != "—") and (winner.strip().lower() == pick_name.strip().lower())
                title = f"R{rn} — winner: {winner} — our pick: {pick_name}" + (" ✅" if hit else "")

                with st.expander(title, expanded=False):
                    st.write(f"**Meeting URL:** {meeting_url}")
                    if res_row.get("place2"):
                        st.write(f"**2nd:** {res_row.get('place2')}")
                    if res_row.get("place3"):
                        st.write(f"**3rd:** {res_row.get('place3')}")
                    if p:
                        st.write(f"**Race URL:** {p.get('race_url') or 'N/A'}")
                        st.write(f"**Picked at:** {p.get('picked_at_iso')}")
                        st.write(f"**Score:** {p.get('pick_score')}")
                        st.write(f"**Key factors:** {p.get('key_factors')}")

                        wb = p.get("why_bullets") or []
                        if wb:
                            st.write("**Why we picked them**")
                            for b in wb:
                                st.write(f"- {b}")

                        hb = p.get("history_bullets") or []
                        if hb:
                            st.write("**History / form snippets**")
                            for b in hb[:10]:
                                st.write(f"- {b}")

                        if p.get("weights"):
                            st.write("**Weights used**")
                            st.json(p.get("weights"))
                        if p.get("conditions"):
                            st.write("**Conditions snapshot**")
                            st.json(p.get("conditions"))
                    else:
                        st.info("No saved pick for this race.")

    if not show_all_results:
        # Only show meetings we have picks for.
        by_meeting: dict[str, list[dict]] = {}
        for p in picks:
            by_meeting.setdefault(p.get("meeting_url", ""), []).append(p)
        for meeting_url, meeting_picks in sorted(by_meeting.items(), key=lambda kv: (kv[1][0].get("venue", ""), kv[0])):
            venue = meeting_picks[0].get("venue") or "Unknown venue"
            code = meeting_picks[0].get("code") or "unknown"
            meeting_date = meeting_picks[0].get("meeting_date") or chosen_date.isoformat()
            render_meeting(meeting_url=meeting_url, venue=venue, code=code, meeting_date=meeting_date)
        return

    # Show meetings for the day (with whatever results are available).
    for c in wanted_codes:
        if c == "greyhound":
            meetings = cached_dog_meetings(chosen_date)
        elif c == "thoroughbred":
            meetings = cached_tb_meetings(chosen_date)
        else:
            meetings = cached_harness_meetings(chosen_date)
        if not meetings:
            continue
        for m in meetings:
            render_meeting(meeting_url=m.meeting_url, venue=m.venue, code=c, meeting_date=chosen_date.isoformat())


@st.dialog("Compression backtest (TB)")
def compression_backtest_dialog(chosen_date: date) -> None:
    st.write("Backtest: measure whether small score gaps (Rank 1 vs 2/3) correlate with place-heavy outcomes.")
    days_back = st.slider("Days to look back (from selected date)", min_value=1, max_value=28, value=7)
    percentile = st.slider("Clustered threshold (percentile)", min_value=10, max_value=50, value=25)
    st.caption(f"Races below P{percentile} of compression_index = clustered; above = clear_edge.")
    if st.button("Run backtest"):
        with st.spinner("Fetching TB meetings, fields, and results..."):
            end_d = chosen_date
            start_d = end_d - timedelta(days=days_back)
            metrics, threshold, summary = run_backtest(
                start_d, end_d, threshold_percentile=float(percentile), ttl_seconds=120
            )
        st.code(format_report(summary), language=None)
        if metrics:
            st.caption(f"Per-race metrics: {len(metrics)} races. Use CLI for full export.")
        else:
            st.info("No TB races with results in this range. Results may not be posted yet.")

@st.dialog("Meetings for selected date")
def show_meetings_dialog(chosen_date: date, meetings: list, fields_by_meeting: dict | None = None) -> None:
    st.write(f"**Date:** {chosen_date.isoformat()}")
    if not meetings:
        st.info("No meetings found for this date.")
        return

    rows = []
    now = datetime.now().astimezone()
    for m in meetings:
        meeting_url = getattr(m, "meeting_url", "")
        mf = (fields_by_meeting or {}).get(meeting_url, {}) if meeting_url else {}
        races = mf.get("races") or []

        # Prefer actual loaded race times (more accurate than our heuristic meeting parser).
        times = [getattr(r, "start_time_local", None) for r in races]
        times = [t for t in times if isinstance(t, time)]
        first_t = min(times) if times else getattr(m, "first_race_time_local", None)
        last_t = max(times) if times else None

        # Status from schedule (best-effort)
        status = getattr(m, "status", "") or "unknown"
        code = getattr(m, "code", "") or ""
        per_race = timedelta(minutes=25 if code == "greyhound" else 35 if code == "thoroughbred" else 30 if code == "harness" else 30)
        if isinstance(first_t, time) and isinstance(last_t, time):
            start_dt = datetime.combine(chosen_date, first_t, tzinfo=now.tzinfo)
            end_dt = datetime.combine(chosen_date, last_t, tzinfo=now.tzinfo) + per_race
            if now < start_dt:
                status = "upcoming"
            elif now > end_dt:
                status = "finished"
            else:
                status = "in_progress"

        t = first_t.strftime("%H:%M") if isinstance(first_t, time) else ""
        races_count = len(races) if races else getattr(m, "num_races", "")
        rows.append(
            {
                "venue": getattr(m, "venue", ""),
                "first race": t,
                "status": status,
                "races": races_count,
                "link": meeting_url,
            }
        )

    st.dataframe(rows, width="stretch", hide_index=True)
    st.caption("Tip: meeting links are included in the table for copy/paste.")


def main() -> None:
    today = date.today()
    # Avoid calling `next_upcoming_meeting()` directly since it refetches thedogs racecards on every rerun,
    # which can trigger intermittent 403 blocks. Instead, compute from our cached meetings list.
    m = None
    try:
        todays = cached_dog_meetings(today)
        if not todays:
            todays = try_fetch_dog_meetings_fallback(today)
        now = datetime.now().astimezone()
        best = None  # (dt, meeting)
        for mtg in todays:
            if mtg.first_race_time_local is None:
                continue
            dt = datetime.combine(mtg.meeting_date, mtg.first_race_time_local, tzinfo=now.tzinfo)
            if dt >= now and (best is None or dt < best[0]):
                best = (dt, mtg)
        if best is not None:
            m = best[1]
    except (FetchError, DogsParseError) as e:
        st.warning(f"Could not compute next meeting from cached meetings: {e}")

    if m is not None:
        # Best-effort "top pick for next race" (greyhounds only; this banner is from thedogs flow).
        pick = None
        try:
            pick = cached_next_greyhound_pick(m.meeting_url, m.meeting_date, m.venue)
        except Exception:
            pick = None

        if pick is not None:
            st.caption(
                f"**Top pick (next race):** {pick['venue']} R{pick['race_no']} ({pick['race_time']}) — "
                f"{pick['pick_name']} (score {pick['pick_score']:.3f})"
            )

            if "show_next_pick_why" not in st.session_state:
                st.session_state.show_next_pick_why = False
            b1, b2 = st.columns([0.25, 0.75], vertical_alignment="center")
            with b1:
                if st.button("Why?", key="btn_next_pick_why"):
                    st.session_state.show_next_pick_why = not st.session_state.show_next_pick_why
            with b2:
                with st.expander(
                    "Why we like this pick (click to expand)",
                    expanded=bool(st.session_state.show_next_pick_why),
                ):
                    for b in pick.get("why_bullets") or []:
                        st.write(f"- {b}")
                    st.caption(f"Race link: {pick.get('race_url')}")

    st.divider()

    with st.expander("Controls", expanded=False):
        ctrl1, ctrl2, ctrl3 = st.columns([1.2, 1.3, 1.4], vertical_alignment="top")

        with ctrl1:
            code = st.selectbox(
                "Code",
                options=[
                    "Greyhounds",
                    "Thoroughbred (All AU)",
                    "Thoroughbred (AU + NZ)",
                    "Harness (NSW)",
                    "All (AU)",
                    "Greyhounds (NZ)",
                    "Harness (NZ)",
                    "Thoroughbred (NZ)",
                    "All (AU+NZ)",
                ],
                index=1,  # default: Thoroughbred (All AU)
            )
            chosen_date = st.date_input("Date", value=today)
            tz_name = st.selectbox("Timezone", options=["Australia/Sydney", "Pacific/Auckland", "Local (server)"], index=0)
            st.session_state.tz_name = tz_name
            if "refresh_nonce" not in st.session_state:
                st.session_state.refresh_nonce = 0
            if st.button("Refresh loaded data", help="Force reload meetings/races (use if a venue seems missing)."):
                st.session_state.refresh_nonce = int(st.session_state.refresh_nonce) + 1
            if code == "Greyhounds":
                st.caption("Meetings + races auto-load for the chosen date.")
            elif code == "Thoroughbred (All AU)":
                st.caption("AU thoroughbred grid via Racing Australia FreeFields — race roster with best picks, backup, and roughie.")
            elif code == "Thoroughbred (AU + NZ)":
                st.caption("Thoroughbreds AU + NZ — race roster grid with best picks (Racing Australia + NZ Racing).")
            elif code == "Harness (NSW)":
                st.caption("Meetings + races auto-load for the chosen date (NSW harness).")
            elif code == "All (AU)":
                st.caption("Unified grid: greyhounds + thoroughbreds + harness (AU), sorted by next to jump.")
            elif code in ("Greyhounds (NZ)", "Harness (NZ)", "Thoroughbred (NZ)"):
                st.caption("NZ meetings (Harness NZ from HRNZ; greyhound/TB stubs when no parser yet).")
            elif code == "All (AU+NZ)":
                st.caption("Unified grid: AU + NZ (greyhounds, thoroughbreds, harness), sorted by next to jump.")
            else:
                st.caption("Meetings + races auto-load for the chosen date.")

        # --- Auto-load meetings for chosen date + code (so buttons below can use meetings) ---
        # Only reload when user has explicitly changed date/code or clicked Refresh (not on Save/Results/etc.)
        _meetings_code = str(code)
        _meetings_date = chosen_date.isoformat() if hasattr(chosen_date, "isoformat") else str(chosen_date)
        _meetings_refresh = int(st.session_state.get("refresh_nonce", 0))
        _m_stored_code = st.session_state.get("meetings_loaded_code")
        _m_stored_date = st.session_state.get("meetings_loaded_date")
        _m_stored_refresh = st.session_state.get("meetings_loaded_refresh", -1)
        _meetings_need_reload = (
            "meetings" not in st.session_state
            or not st.session_state.meetings
            or _m_stored_code is None
            or _m_stored_code != _meetings_code
            or _m_stored_date != _meetings_date
            or _m_stored_refresh != _meetings_refresh
        )
        if "meetings" not in st.session_state:
            st.session_state.meetings = []
        if _meetings_need_reload:
            try:
                meetings = get_meetings_for_code(code, chosen_date, _meetings_refresh)
                st.session_state.meetings = meetings
                st.session_state.meetings_loaded_key = (_meetings_code, _meetings_date, _meetings_refresh)
                st.session_state.meetings_loaded_code = _meetings_code
                st.session_state.meetings_loaded_date = _meetings_date
                st.session_state.meetings_loaded_refresh = _meetings_refresh
                # Persist meeting list for tracking / reload without live source.
                if meetings and chosen_date:
                    db_persist_daily_meetings(chosen_date, _meetings_code, meetings)
            except (FetchError, DogsParseError, RacingAUSParseError, HarnessParseError, HrnzNzParseError, NzRacingParseError) as e:
                st.session_state.meetings = []
                st.session_state.meetings_loaded_key = (_meetings_code, _meetings_date, _meetings_refresh)
                st.session_state.meetings_loaded_code = _meetings_code
                st.session_state.meetings_loaded_date = _meetings_date
                st.session_state.meetings_loaded_refresh = _meetings_refresh
                st.error(f"Could not load meetings: {e}")

        meetings = st.session_state.meetings

        with ctrl3:
            st.markdown("**Tracking database**")
            _db = db_status()
            st.caption(f"`{_db.get('path', '')}`")
            st.caption(
                f"Picks **{_db.get('picks', 0)}** · Results **{_db.get('results', 0)}** · "
                f"Jockey rides **{_db.get('jockey_rides', 0)}** · "
                f"Fields **{_db.get('daily_fields', 0)}** · Meetings **{_db.get('daily_meetings', 0)}** · "
                f"HTTP cache **{_db.get('cache', 0)}**"
            )
            _pbd = _db.get("picks_by_date") or []
            if _pbd:
                st.caption("Picks by date: " + ", ".join(f"{d}×{n}" for d, n in _pbd[:5]))
            _rbd = _db.get("results_by_date") or []
            if _rbd:
                st.caption("Results by date: " + ", ".join(f"{d}×{n}" for d, n in _rbd[:5]))
            st.caption(
                "Roster autosaves best / if scratched / roughie + scores. "
                "Fields & meetings save on load. Results save when you open Results / Daily review "
                "(and sync jockey rides from the field)."
            )
            if st.button("Backfill jockey rides", help="Rebuild ride ledger from all stored results + fields"):
                with st.spinner("Backfilling jockey rides..."):
                    bf = backfill_jockey_rides()
                st.success(f"Backfilled **{bf.get('rides', 0)}** rides across **{bf.get('meetings', 0)}** meeting result sets.")
                st.rerun()

        with ctrl1:
            show_meetings = st.button("Show meetings for this date", disabled=(not meetings))
            if show_meetings:
                show_meetings_dialog(chosen_date, meetings, st.session_state.get("fields_by_meeting"))
            show_review = st.button("Daily review (winners vs our picks)")
            if show_review:
                daily_review_dialog(chosen_date)
            show_compression = st.button("Compression backtest (TB)")
            if show_compression:
                compression_backtest_dialog(chosen_date)

    # Ensure meetings/refresh_nonce in scope for rest of page (same values as set inside expander)
    if "meetings" not in st.session_state:
        st.session_state.meetings = []
    refresh_nonce = int(st.session_state.get("refresh_nonce", 0))
    meetings = st.session_state.meetings

    # --- Auto-load races (and runners for TB/harness) for ALL venues; branch on m.code for mixed All (AU) ---
    # Only reload when user has explicitly changed date/code or clicked Refresh (not on Save/Results/Update race)
    _code_str = str(code)
    _date_str = chosen_date.isoformat() if hasattr(chosen_date, "isoformat") else str(chosen_date)
    _stored_code = st.session_state.get("roster_loaded_code")
    _stored_date = st.session_state.get("roster_loaded_date")
    _stored_refresh = st.session_state.get("roster_loaded_refresh", -1)
    _need_reload = (
        "fields_by_meeting" not in st.session_state
        or not st.session_state.fields_by_meeting
        or _stored_code is None
        or _stored_code != _code_str
        or _stored_date != _date_str
        or _stored_refresh != refresh_nonce
    )
    if "fields_by_meeting" not in st.session_state:
        st.session_state.fields_by_meeting = {}
    if _need_reload:
        fields_by_meeting: dict[str, dict] = {}
        if meetings:
            with st.spinner("Loading races for all venues (DB or cache)..."):
                prog = st.progress(0)
                total = len(meetings)
                for i, m in enumerate(meetings, start=1):
                    try:
                        m_code = getattr(m, "code", "") or ""
                        country = (getattr(m, "extra", {}) or {}).get("country") or "AU"
                        is_nz = country == "NZ" or getattr(m, "source", "") == "hrnz_nz"
                        is_nz_dog = country == "NZ" or getattr(m, "source", "") == "grnz_nz"

                        # 1) Live fetch (Acceptances may omit earlier races once they've run)
                        live_tuple: tuple | None = None
                        try:
                            if m_code == "greyhound":
                                if is_nz_dog:
                                    races, runners_by_race = cached_nz_dog_fields(m.meeting_url, m.meeting_date)
                                    live_tuple = (races, runners_by_race, {})
                                else:
                                    races = cached_dog_races(m.meeting_url)
                                    live_tuple = (races, None, {})
                            elif m_code == "thoroughbred":
                                if is_nz:
                                    races, runners_by_race, meta = cached_nz_tb_fields(m.meeting_url, m.meeting_date)
                                else:
                                    races, runners_by_race, meta = cached_tb_fields(m.meeting_url, refresh_nonce)
                                live_tuple = (races, runners_by_race, meta or {})
                            elif m_code == "harness":
                                if is_nz:
                                    races, runners_by_race = cached_nz_harness_fields(m.meeting_url, m.meeting_date)
                                else:
                                    races, runners_by_race = cached_harness_fields(m.meeting_url, m.meeting_date)
                                live_tuple = (races, runners_by_race, {})
                        except Exception:
                            live_tuple = None

                        # 2) Stored daily card (keeps R1/R2… after the live source drops them)
                        db_tuple: tuple | None = None
                        if chosen_date:
                            db_data = db_load_daily_fields(chosen_date, m.meeting_url)
                            if db_data is not None:
                                if len(db_data) == 2:
                                    db_tuple = (db_data[0], db_data[1], {})
                                else:
                                    db_tuple = (db_data[0], db_data[1], db_data[2] if len(db_data) > 2 else {})

                        # 3) Session card (in-memory full card from earlier in the day)
                        current = st.session_state.fields_by_meeting.get(m.meeting_url)
                        session_tuple = None
                        if current and (current.get("races") or current.get("runners_by_race")):
                            session_tuple = (
                                current.get("races") or [],
                                current.get("runners_by_race"),
                                current.get("meta") or {},
                            )

                        # Merge order: session/db first (historical), then live on top (never shrink).
                        merged: tuple = ([], {}, {})
                        for part in (session_tuple, db_tuple, live_tuple):
                            if part is None:
                                continue
                            merged = db_merge_meeting_fields(merged, part) if (merged[0] or merged[1]) else part

                        fields_by_meeting[m.meeting_url] = {
                            "races": merged[0],
                            "runners_by_race": merged[1],
                            "meta": merged[2] if len(merged) > 2 else {},
                        }
                        if chosen_date and (merged[0] or merged[1]):
                            db_persist_daily_fields(chosen_date, m.meeting_url, merged)
                    except Exception as e:
                        fields_by_meeting[m.meeting_url] = {"races": [], "runners_by_race": {}, "meta": {}}
                        st.warning(f"Could not load races for {m.venue}: {e}")
                    prog.progress(int(i / max(total, 1) * 100))
                prog.empty()
        # Never shrink: merge with existing session state so grid click / rerun never drops races
        existing = st.session_state.get("fields_by_meeting") or {}
        for meeting_url, mf in existing.items():
            if not mf or not (mf.get("races") or mf.get("runners_by_race")):
                continue
            cur = fields_by_meeting.get(meeting_url)
            cur_tuple = (cur.get("races") or [], cur.get("runners_by_race") or {}, cur.get("meta") or {}) if cur else ([], {}, {})
            old_tuple = (mf.get("races") or [], mf.get("runners_by_race") or {}, mf.get("meta") or {})
            merged = db_merge_meeting_fields(old_tuple, cur_tuple)
            fields_by_meeting[meeting_url] = {"races": merged[0], "runners_by_race": merged[1], "meta": merged[2]}
        st.session_state.fields_by_meeting = fields_by_meeting
        st.session_state.fields_loaded_key = (_code_str, _date_str, refresh_nonce)
        st.session_state.roster_loaded_code = _code_str
        st.session_state.roster_loaded_date = _date_str
        st.session_state.roster_loaded_refresh = refresh_nonce
        # Freeze grid data: merge with previous snapshot so we never drop previous races.
        # Important: merge across Refresh (nonce change) for the same code+date, otherwise
        # just-run races disappear when Acceptances/Form stop listing them.
        new_snapshot = copy.deepcopy(fields_by_meeting)
        prev = st.session_state.get("roster_snapshot")
        prev_key = st.session_state.get("roster_snapshot_key")
        if (
            prev
            and isinstance(prev_key, tuple)
            and len(prev_key) >= 2
            and prev_key[0] == _code_str
            and prev_key[1] == _date_str
        ):
            for meeting_url, mf in (prev or {}).items():
                if not mf or not (mf.get("races") or mf.get("runners_by_race")):
                    continue
                cur = new_snapshot.get(meeting_url)
                cur_t = (cur.get("races") or [], cur.get("runners_by_race") or {}, cur.get("meta") or {}) if cur else ([], {}, {})
                old_t = (mf.get("races") or [], mf.get("runners_by_race") or {}, mf.get("meta") or {})
                merged = db_merge_meeting_fields(old_t, cur_t)
                new_snapshot[meeting_url] = {"races": merged[0], "runners_by_race": merged[1], "meta": merged[2]}
        st.session_state.roster_snapshot = new_snapshot
        st.session_state.roster_snapshot_key = (_code_str, _date_str, refresh_nonce)

    fields_by_meeting = st.session_state.fields_by_meeting

    # Backfill jockey silks + last-start class for AU TB meetings (Form.aspx).
    _silks_key = (_code_str, _date_str, refresh_nonce, "form_extras_v1")
    if st.session_state.get("silks_enriched_key") != _silks_key:
        st.session_state.silks_enriched_key = _silks_key
        st.session_state.silks_enriched_urls = set()
    _silks_done = st.session_state.setdefault("silks_enriched_urls", set())
    _need_silk = [
        (url, mf)
        for url, mf in (fields_by_meeting or {}).items()
        if url not in _silks_done
        and "racingaustralia.horse" in str(url)
        and (
            tb_runners_missing_silks((mf or {}).get("runners_by_race"))
            or tb_runners_missing_last_class((mf or {}).get("runners_by_race"))
        )
    ]
    if _need_silk:
        with st.spinner(f"Loading form extras (silks / class) for {len(_need_silk)} meeting(s)..."):
            for meeting_url, mf in _need_silk:
                try:
                    runners_by = enrich_runners_with_silks(
                        meeting_url,
                        (mf or {}).get("runners_by_race") or {},
                        ttl_seconds=600,
                        force=True,
                    )
                    mf = dict(mf or {})
                    mf["runners_by_race"] = runners_by
                    fields_by_meeting[meeting_url] = mf
                    st.session_state.fields_by_meeting[meeting_url] = mf
                    snap = st.session_state.get("roster_snapshot") or {}
                    if snap is not None:
                        snap[meeting_url] = mf
                        st.session_state.roster_snapshot = snap
                    if chosen_date:
                        db_persist_daily_fields(
                            chosen_date,
                            meeting_url,
                            (mf.get("races") or [], runners_by, mf.get("meta") or {}),
                        )
                    # Picks cache may have been built before form extras existed — force recompute.
                    st.session_state.roster_picks_cache = {}
                    st.session_state.pop("roster_autosaved_key", None)
                except Exception:
                    pass
                _silks_done.add(meeting_url)
        st.session_state.silks_enriched_urls = _silks_done

    # --- Roster grid codes: All modes + Thoroughbred (race list with best picks) ---
    # Grid uses snapshot merged with current session so we never show fewer races (e.g. previous R1/R2 from DB stay visible)
    _uses_roster_grid = code in ("All (AU)", "All (AU+NZ)") or str(code).startswith("Thoroughbred")
    _snapshot_key = (_code_str, _date_str, refresh_nonce)
    if (
        _uses_roster_grid
        and st.session_state.get("roster_snapshot_key") == _snapshot_key
        and st.session_state.get("roster_snapshot")
    ):
        snapshot = st.session_state.roster_snapshot
        current = st.session_state.fields_by_meeting
        merged_fbm = {}
        for meeting_url in set((snapshot or {}).keys()) | set((current or {}).keys()):
            old_mf = (snapshot or {}).get(meeting_url)
            new_mf = (current or {}).get(meeting_url)
            old_t = (
                (old_mf or {}).get("races") or [],
                (old_mf or {}).get("runners_by_race") or {},
                (old_mf or {}).get("meta") or {},
            )
            new_t = (
                (new_mf or {}).get("races") or [],
                (new_mf or {}).get("runners_by_race") or {},
                (new_mf or {}).get("meta") or {},
            )
            merged = db_merge_meeting_fields(old_t, new_t)
            # Prefer runners that already have silks when merging (snapshot may be older).
            merged_runners = merged[1] or {}
            if tb_runners_missing_silks(merged_runners) and not tb_runners_missing_silks(new_t[1]):
                merged_runners = new_t[1]
            elif tb_runners_missing_silks(merged_runners) and not tb_runners_missing_silks(old_t[1]):
                merged_runners = old_t[1]
            merged_fbm[meeting_url] = {"races": merged[0], "runners_by_race": merged_runners, "meta": merged[2]}
        fields_by_meeting = merged_fbm

    if code in ("All (AU)", "All (AU+NZ)"):
        tz_name_au = st.session_state.get("tz_name") or "Australia/Sydney"
        app_tz_au = None
        if tz_name_au and tz_name_au != "Local (server)":
            try:
                app_tz_au = ZoneInfo(tz_name_au)
            except Exception:
                pass
        now_au = datetime.now(app_tz_au).astimezone() if app_tz_au else datetime.now().astimezone()

        # Sky Next Up panel (schedule.skyracing.com.au overlay)
        try:
            sky_list = cached_sky_schedule(chosen_date)
            if sky_list:
                sky_next = sorted(sky_list, key=lambda x: (x.get("channel", ""), x.get("venue", ""), x.get("race_no", 0)))[:5]
                with st.expander("Sky Next Up (Sky 1/2 schedule)", expanded=False):
                    for s in sky_next:
                        ch = s.get("channel", "")
                        ven = s.get("venue", "")
                        rn = s.get("race_no", "")
                        dt_sky = s.get("dt_app_tz") or s.get("dt_local")
                        t_str = dt_sky.strftime("%H:%M") if dt_sky else "—"
                        st.caption(f"**{ch}** {ven} R{rn} — {t_str}")
        except Exception:
            pass

        def _tz_tb_au(m) -> ZoneInfo | None:
            if getattr(m, "code", "") != "thoroughbred":
                return None
            # NZ thoroughbred: Pacific/Auckland
            if (getattr(m, "extra", {}) or {}).get("country") == "NZ":
                return ZoneInfo("Pacific/Auckland")
            st_code = (getattr(m, "extra", {}) or {}).get("state") or ""
            st_code = str(st_code).upper().strip()
            if st_code in {"NSW", "VIC", "TAS", "ACT"}:
                return ZoneInfo("Australia/Sydney")
            if st_code == "QLD":
                return ZoneInfo("Australia/Brisbane")
            if st_code == "SA":
                return ZoneInfo("Australia/Adelaide")
            if st_code == "NT":
                return ZoneInfo("Australia/Darwin")
            if st_code == "WA":
                return ZoneInfo("Australia/Perth")
            return None

        unified_rows: list[dict] = []
        for m in meetings:
            mf = fields_by_meeting.get(getattr(m, "meeting_url", ""), {}) or {}
            races_au = mf.get("races") or []
            m_code = getattr(m, "code", "") or ""
            country = (getattr(m, "extra", {}) or {}).get("country") or "AU"
            venue_au = getattr(m, "venue", "") or ""
            state_code = (getattr(m, "extra", {}) or {}).get("state") or ""
            if m_code == "thoroughbred" and state_code:
                venue_au = f"{venue_au} ({state_code})"
            mtg_tz_au = _tz_tb_au(m) if m_code == "thoroughbred" else _tz_for_greyhound_meeting(m) if m_code == "greyhound" else None
            if mtg_tz_au is None and country == "NZ":
                mtg_tz_au = ZoneInfo("Pacific/Auckland")
            if mtg_tz_au is None and app_tz_au:
                mtg_tz_au = app_tz_au
            per_race_au = timedelta(minutes=25 if m_code == "greyhound" else 35 if m_code == "thoroughbred" else 30)
            for r in races_au:
                start_t = getattr(r, "start_time_local", None)
                dt_au = None
                approx_au = False
                if isinstance(start_t, time):
                    dt_local = datetime.combine(chosen_date, start_t, tzinfo=(mtg_tz_au or now_au.tzinfo))
                    dt_au = dt_local.astimezone(app_tz_au) if app_tz_au else dt_local
                elif m_code == "greyhound":
                    first_t = getattr(m, "first_race_time_local", None)
                    rn = getattr(r, "race_no", None)
                    if isinstance(first_t, time) and isinstance(rn, int) and rn >= 1:
                        dt_au = datetime.combine(chosen_date, first_t, tzinfo=(mtg_tz_au or now_au.tzinfo)) + per_race_au * (rn - 1)
                        if app_tz_au:
                            dt_au = dt_au.astimezone(app_tz_au)
                        approx_au = True
                status_au = "unknown"
                if dt_au is not None:
                    if now_au < dt_au:
                        status_au = "upcoming"
                    elif now_au <= dt_au + per_race_au:
                        status_au = "in_progress"
                    else:
                        status_au = "finished"
                time_str = (f"~{dt_au.strftime('%H:%M')}" if approx_au and dt_au else (dt_au.strftime("%H:%M") if dt_au else ""))
                unified_rows.append({
                    "dt": dt_au,
                    "country": country,
                    "code": m_code,
                    "venue": venue_au,
                    "race_no": getattr(r, "race_no", None),
                    "distance": getattr(r, "distance_m", None) or "",
                    "name": getattr(r, "name", "") or "",
                    "class": parse_race_class_label(getattr(r, "name", "") or "") if m_code == "thoroughbred" else "",
                    "status": status_au,
                    "url": str(getattr(r, "race_url", "") or ""),
                    "meeting_url": getattr(m, "meeting_url", ""),
                    "time_disp": time_str,
                })
        # Sort by dt ascending (unknown times last).
        unified_rows.sort(key=lambda x: (x["dt"] is None, x["dt"] or datetime.max.replace(tzinfo=now_au.tzinfo)))
        # Roster AG Grid inline (same as Race roster dialog, no button needed).
        render_roster_content(
            chosen_date=chosen_date,
            code_label=code,
            meetings=meetings,
            fields_by_meeting=fields_by_meeting,
            open_nonce=0,
        )
        st.divider()
    elif str(code).startswith("Thoroughbred"):
        # Same race roster + best picks grid as All modes, filtered to thoroughbred only.
        render_roster_content(
            chosen_date=chosen_date,
            code_label=code,
            meetings=meetings,
            fields_by_meeting=fields_by_meeting,
            open_nonce=0,
        )
        st.divider()

    # --- Single-code: Next race banner + venue/race selector + rank flow ---
    # Thoroughbred uses the roster grid above (with best_pick / if_scratched / roughie).
    if not _uses_roster_grid:
        tz_name2 = st.session_state.get("tz_name") or "Australia/Sydney"
        tz2 = None
        if tz_name2 and tz_name2 != "Local (server)":
            try:
                tz2 = ZoneInfo(tz_name2)
            except Exception:
                tz2 = None
        now2 = datetime.now(tz2).astimezone() if tz2 is not None else datetime.now().astimezone()
        now2_str = now2.strftime("%H:%M")
        code_id = (
            "greyhound" if code == "Greyhounds" or code == "Greyhounds (NZ)"
            else "thoroughbred" if code.startswith("Thoroughbred")
            else "harness"
        )
        per_race2 = timedelta(minutes=25 if code_id == "greyhound" else 35 if code_id == "thoroughbred" else 30)

        def _tz_for_tb_meeting(m) -> ZoneInfo | None:
            try:
                if getattr(m, "code", "") != "thoroughbred":
                    return None
                st_code = (getattr(m, "extra", {}) or {}).get("state") or ""
                st_code = str(st_code).upper().strip()
                if st_code in {"NSW", "VIC", "TAS", "ACT"}:
                    return ZoneInfo("Australia/Sydney")
                if st_code == "QLD":
                    return ZoneInfo("Australia/Brisbane")
                if st_code == "SA":
                    return ZoneInfo("Australia/Adelaide")
                if st_code == "NT":
                    return ZoneInfo("Australia/Darwin")
                if st_code == "WA":
                    return ZoneInfo("Australia/Perth")
            except Exception:
                return None
            return None

        best_next: tuple[datetime, str, int | None, bool, str] | None = None  # (dt, venue, race_no, approx, race_url)
        for m in meetings:
            mf = fields_by_meeting.get(getattr(m, "meeting_url", ""), {}) or {}
            races2 = mf.get("races") or []
            mtg_tz = _tz_for_tb_meeting(m) if code_id == "thoroughbred" else _tz_for_greyhound_meeting(m) if code_id == "greyhound" else None
            if mtg_tz is None and (getattr(m, "extra", {}) or {}).get("country") == "NZ":
                mtg_tz = ZoneInfo("Pacific/Auckland")
            if mtg_tz is None and tz2 is not None:
                mtg_tz = tz2
            for r in races2:
                start_t = getattr(r, "start_time_local", None)
                dt = None
                approx = False
                if isinstance(start_t, time):
                    dt = datetime.combine(chosen_date, start_t, tzinfo=(mtg_tz or now2.tzinfo))
                elif code_id == "greyhound":
                    first_t = getattr(m, "first_race_time_local", None)
                    rn = getattr(r, "race_no", None)
                    if isinstance(first_t, time) and isinstance(rn, int) and rn >= 1:
                        dt = datetime.combine(chosen_date, first_t, tzinfo=(mtg_tz or now2.tzinfo)) + per_race2 * (rn - 1)
                        approx = True

                if dt is None or dt < now2:
                    continue

                if best_next is None or dt < best_next[0]:
                    best_next = (
                        dt,
                        getattr(m, "venue", "") or "",
                        getattr(r, "race_no", None),
                        approx,
                        str(getattr(r, "race_url", "") or ""),
                    )

        if best_next is not None:
            dt, venue2, race_no2, approx2, _url2 = best_next
            mins2 = int((dt - now2).total_seconds() // 60)
            t2 = (f"~{dt.strftime('%H:%M')}" if approx2 else dt.strftime("%H:%M"))
            approx_note2 = " (approx)" if approx2 else ""
            rno_disp = f"R{race_no2}" if isinstance(race_no2, int) else "R?"
            st.success(
                f"**Next race ({code})**: {venue2} {rno_disp} at {t2} (in ~{mins2} min){approx_note2} — "
                f"**current time (app):** {now2_str} ({tz_name2})"
            )
        else:
            st.info(f"Next race ({code}): N/A (no upcoming races with known times loaded).")

        with ctrl2:
            venue_options = ["(select)"] + [m.venue for m in meetings]
            venue = st.selectbox("Venue", options=venue_options, index=0)

        selected_meeting = next((m for m in meetings if m.venue == venue), None) if venue != "(select)" else None
        meeting_fields = fields_by_meeting.get(selected_meeting.meeting_url, {}) if selected_meeting else {}
        races = meeting_fields.get("races", []) if selected_meeting else []
        meeting_meta = meeting_fields.get("meta", {}) if selected_meeting else {}

        if selected_meeting is not None:
            st.subheader("Conditions")
            tc = meeting_meta.get("track_condition")
            w = meeting_meta.get("weather")
            pen = meeting_meta.get("penetrometer")
            if tc or w or pen:
                st.write(
                    f"**Track condition:** {tc or 'N/A'}  \n"
                    f"**Weather:** {w or 'N/A'}  \n"
                    f"**Penetrometer:** {pen or 'N/A'}"
                )
            else:
                st.caption("Track/weather conditions: N/A for this code/source (v0).")

            with st.expander("External live weather (optional)", expanded=False):
                st.caption("Pulled from a public weather endpoint (best-effort). Informational only.")
                snap = cached_venue_weather(selected_meeting.venue)
                if snap is None:
                    st.info("No location mapping for this venue yet (v0).")
                else:
                    st.write(
                        f"**Temp:** {snap.temperature_c}°C  \n"
                        f"**Humidity:** {snap.relative_humidity_pct}%  \n"
                        f"**Precip:** {snap.precipitation_mm} mm  \n"
                        f"**Wind:** {snap.wind_speed_kmh} km/h  \n"
                        f"**Provider:** {snap.provider}"
                    )

        with ctrl3:
            race_labels = ["(select)"]
            for r in races:
                tm = f" {r.start_time_local.strftime('%H:%M')}" if r.start_time_local else ""
                race_labels.append(f"R{r.race_no}{tm}")
            race_label = st.selectbox("Race", options=race_labels, index=0)

        selected_race = None
        if race_label != "(select)":
            try:
                no = int(race_label.split()[0].lstrip("R"))
                selected_race = next((r for r in races if r.race_no == no), None)
            except Exception:
                selected_race = None

        if selected_meeting is not None and selected_race is not None and selected_race.start_time_local is not None:
            rws = cached_race_weather(selected_meeting.venue, selected_meeting.meeting_date, selected_race.start_time_local)
            if rws is not None:
                st.caption(
                    f"Race-time weather (approx): precip={rws.precipitation_mm}mm, wind={rws.wind_speed_kmh}km/h, humidity={rws.relative_humidity_pct}% (Open‑Meteo)"
                )

        st.subheader("Scoring controls")
        w1, w2, w3, w4 = st.columns([1, 1, 1, 1], vertical_alignment="center")
        with w1:
            auto_weights = st.toggle("Auto (AI) weights", value=True)
            box_w = st.slider("Box / draw weight", 0.0, 1.0, 0.33, 0.01, disabled=auto_weights)
        with w2:
            form_w = st.slider("Recent form weight", 0.0, 1.0, 0.34, 0.01, disabled=auto_weights)
        with w3:
            early_w = st.slider("Early speed / class proxy weight", 0.0, 1.0, 0.33, 0.01, disabled=auto_weights)
        with w4:
            explain_mode = st.toggle("Explain mode (detailed)", value=False)
            show_debug = st.toggle("Show debug info", value=False)

        bw, fw, ew = normalize_weights(box_w, form_w, early_w)
        st.caption(f"Normalized weights: draw={bw:.2f}, form={fw:.2f}, proxy={ew:.2f}")

        st.subheader("Action")
        rank = st.button("Rank Runners", disabled=(selected_race is None))

        if rank and selected_race is not None:
            try:
                if code == "Greyhounds":
                    runners = cached_dog_runners(selected_race.race_url)
                else:
                    runners_by = meeting_fields.get("runners_by_race") or {}
                    runners = runners_by.get(selected_race.race_no, [])

                if auto_weights:
                    # Use per-race forecast if possible; otherwise current.
                    wx = None
                    if selected_meeting is not None and selected_race is not None:
                        wx = cached_race_weather(selected_meeting.venue, selected_meeting.meeting_date, selected_race.start_time_local)
                    bw, fw, ew, rationale = suggest_auto_weights(
                        runners,
                        weather=wx,
                        track_condition=meeting_meta.get("track_condition") if code.startswith("Thoroughbred") else None,
                    )
                    if show_debug:
                        st.write("**Auto-weight rationale**")
                        for line in rationale:
                            st.write(f"- {line}")

                ranked = rank_runners(
                    runners,
                    box_weight=bw if auto_weights else box_w,
                    form_weight=fw if auto_weights else form_w,
                    early_weight=ew if auto_weights else early_w,
                    weather=(
                        cached_race_weather(selected_meeting.venue, selected_meeting.meeting_date, selected_race.start_time_local)
                        if selected_meeting is not None and selected_race is not None
                        else None
                    ),
                    track_condition=meeting_meta.get("track_condition") if code.startswith("Thoroughbred") else None,
                    explain_mode="detailed" if explain_mode else "short",
                )
            except Exception as e:
                st.error(f"Could not rank runners: {e}")
                return

            # Save context for optional journaling
            st.session_state.last_rank_context = {
                "code_label": code,
                "code": ("greyhound" if code == "Greyhounds" else "thoroughbred" if code.startswith("Thoroughbred") else "harness"),
                "chosen_date": chosen_date,
                "selected_meeting": selected_meeting,
                "selected_race": selected_race,
                "meeting_meta": meeting_meta,
                "auto_weights": auto_weights,
                "weights_used": {
                    "draw": float(bw if auto_weights else box_w),
                    "form": float(fw if auto_weights else form_w),
                    "proxy": float(ew if auto_weights else early_w),
                },
            }
            st.session_state.last_rank_outputs = {"ranked": ranked, "runners": runners}

            st.subheader("Ranked runners")
            rows = []
            # Fast lookup for extra runner fields (e.g. age/sex for thoroughbreds)
            runner_by_name = {getattr(x, "name", ""): x for x in (runners or []) if getattr(x, "name", "")}
            if code.startswith("Thoroughbred"):
                silk_bits = []
                for rr in ranked:
                    r0 = runner_by_name.get(rr.name)
                    silk = str(getattr(r0, "silk_url", None) or "") if r0 is not None else ""
                    if silk:
                        silk_bits.append(
                            f'<span style="display:inline-flex;align-items:center;gap:4px;margin:2px 10px 2px 0;">'
                            f'<img src="{html.escape(silk)}" height="24" referrerpolicy="no-referrer" />'
                            f'<span>{rr.rank}. {html.escape(rr.name)}</span></span>'
                        )
                if silk_bits:
                    st.markdown(
                        '<div style="line-height:1.8;">' + "".join(silk_bits) + "</div>",
                        unsafe_allow_html=True,
                    )
            for rr in ranked:
                r0 = runner_by_name.get(rr.name)
                age = getattr(r0, "age", None) if r0 is not None else None
                rows.append(
                    {
                        "rank": rr.rank,
                        ("dog name" if code == "Greyhounds" else "runner"): rr.name,
                        **({"age": age} if code.startswith("Thoroughbred") else {}),
                        ("box" if code == "Greyhounds" else rr.draw_label): rr.draw,
                        "score": round(rr.score, 3),
                        "short key factors": rr.key_factors,
                    }
                )
            st.dataframe(rows, width="stretch", hide_index=True)

            save_pick = st.button("Save pick to Daily review", disabled=(selected_meeting is None))
            if save_pick and selected_meeting is not None:
                try:
                    top = ranked[0] if ranked else None
                    if top is None:
                        raise RuntimeError("No ranked runners to save.")

                    # Best-effort: capture some history bullets for the top pick.
                    r_obj = next((r for r in runners if getattr(r, "name", None) == top.name), None)
                    hist: list[str] = []
                    if r_obj is not None:
                        if getattr(r_obj, "code", None) == "thoroughbred" and getattr(r_obj, "profile_url", None):
                            hist = cached_tb_history(r_obj.profile_url)
                        else:
                            hist = history_bullets_for_runner(r_obj)

                    wx = None
                    if selected_race.start_time_local is not None:
                        wx = cached_race_weather(selected_meeting.venue, selected_meeting.meeting_date, selected_race.start_time_local)

                    entry = make_pick_entry(
                        meeting_date=selected_meeting.meeting_date,
                        code=("greyhound" if code == "Greyhounds" else "thoroughbred" if code.startswith("Thoroughbred") else "harness"),
                        venue=selected_meeting.venue,
                        meeting_url=selected_meeting.meeting_url,
                        race_no=int(selected_race.race_no),
                        race_name=getattr(selected_race, "name", f"Race {selected_race.race_no}") or f"Race {selected_race.race_no}",
                        race_url=selected_race.race_url,
                        pick_name=top.name,
                        pick_draw=top.draw,
                        pick_score=float(top.score),
                        key_factors=top.key_factors,
                        why_bullets=list(top.why_bullets),
                        history_bullets=hist[:12],
                        weights={
                            "auto_weights": bool(auto_weights),
                            "draw_weight": float(bw if auto_weights else box_w),
                            "form_weight": float(fw if auto_weights else form_w),
                            "proxy_weight": float(ew if auto_weights else early_w),
                        },
                        conditions={
                            "track_condition": meeting_meta.get("track_condition"),
                            "meeting_weather": meeting_meta.get("weather"),
                            "penetrometer": meeting_meta.get("penetrometer"),
                            "race_weather": (wx.__dict__ if wx is not None else None),
                        },
                    )
                    upsert_pick(entry)
                    db_save_pick(
                        date.fromisoformat(entry.meeting_date),
                        entry.meeting_url,
                        entry.code,
                        entry.race_no,
                        entry.venue,
                        entry.race_name or f"R{entry.race_no}",
                        entry.pick_name,
                        backup="",
                        pick_data=asdict(entry),
                    )
                    st.success("Saved. Open 'Daily review' to see winner vs pick once results are posted.")
                except Exception as e:
                    st.error(f"Could not save pick: {e}")

            top_n = min(5, len(ranked))
            st.subheader(f"Why this could win (top {top_n})")
            for rr in ranked[:top_n]:
                with st.expander(f"#{rr.rank} {rr.name} (score {rr.score:.3f})", expanded=(rr.rank == 1)):
                    for b in rr.why_bullets:
                        st.write(f"- {b}")

                    # Historical/contextual bullets (best-effort).
                    if explain_mode:
                        r_obj = next((r for r in runners if getattr(r, "name", None) == rr.name), None)
                        hist: list[str] = []
                        if r_obj is not None:
                            if getattr(r_obj, "code", None) == "thoroughbred" and getattr(r_obj, "profile_url", None):
                                hist = cached_tb_history(r_obj.profile_url)
                            else:
                                hist = history_bullets_for_runner(r_obj)
                        if hist:
                            st.write("**History / form snippets**")
                            for h in hist[:8]:
                                st.write(f"- {h}")
                    if show_debug:
                        st.write("**Debug**")
                        st.json(rr.debug)


if __name__ == "__main__":
    main()

