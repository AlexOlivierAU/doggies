from __future__ import annotations

import re
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
from parse_racingaustralia import fetch_meetings_for_date as fetch_tb_meetings_for_date
from parse_racingaustralia import fetch_races_and_runners_for_meeting as fetch_tb_races_and_runners
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
from journal import load_picks, make_pick_entry, upsert_pick
from review import fetch_results_for_meeting
from backtest_compression import run_backtest, format_report


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
    # Longer TTL here to avoid repeatedly hitting thedogs on Streamlit reruns
    # (which can trigger temporary 403 blocks).
    return fetch_dog_meetings_for_date(d, ttl_seconds=30 * 60)


@st.cache_data(show_spinner=False)
def cached_dog_races(meeting_url: str) -> list:
    return fetch_dog_races_for_meeting(meeting_url, ttl_seconds=30 * 60)


def cached_dog_runners(race_url: str) -> list:
    """Fetch greyhound runners for a race. No Streamlit cache (return value not pickle-safe); fetch layer still caches HTML."""
    return fetch_dog_runners_for_race(race_url, ttl_seconds=10 * 60)


@st.cache_data(show_spinner=False)
def cached_tb_meetings(d: date, refresh_nonce: int = 0) -> list:
    # refresh_nonce is a cache-buster (Streamlit cache is otherwise sticky across code changes)
    _ = refresh_nonce
    return fetch_tb_meetings_for_date(d)


@st.cache_data(show_spinner=False)
def cached_tb_fields(meeting_url: str, refresh_nonce: int = 0) -> tuple[list, dict, dict]:
    _ = refresh_nonce
    return fetch_tb_races_and_runners(meeting_url)


@st.cache_data(show_spinner=False)
def cached_harness_meetings(d: date) -> list:
    return fetch_harness_meetings_for_date(d)


@st.cache_data(show_spinner=False)
def cached_harness_fields(meeting_url: str, meeting_date: date) -> tuple[list, dict]:
    return fetch_harness_races_and_runners(meeting_url, meeting_date)


# --- NZ: Harness NZ (HRNZ) implemented; greyhound via GRNZ (Hatrick Straight, etc.) ---
@st.cache_data(show_spinner=False)
def cached_nz_dog_meetings(d: date) -> list:
    try:
        return fetch_grnz_meetings_for_date(d, ttl_seconds=30 * 60)
    except Exception:
        return []


@st.cache_data(show_spinner=False)
def cached_nz_dog_fields(meeting_url: str, meeting_date: date) -> tuple[list, dict]:
    """NZ greyhound: races + runners from GRNZ (or placeholder + empty on failure)."""
    try:
        return fetch_grnz_races_and_runners_for_meeting(meeting_url, meeting_date, ttl_seconds=30 * 60)
    except Exception:
        races = fetch_grnz_races_for_meeting(meeting_url, meeting_date)
        runners_by_race = {getattr(r, "race_no", i): [] for i, r in enumerate(races or [], 1)}
        return (races or [], runners_by_race)


@st.cache_data(show_spinner=False)
def cached_nz_harness_meetings(d: date) -> list:
    try:
        return fetch_nz_harness_meetings_for_date(d, ttl_seconds=30 * 60)
    except Exception:
        return []


@st.cache_data(show_spinner=False)
def cached_nz_harness_fields(meeting_url: str, meeting_date: date) -> tuple[list, dict]:
    return fetch_nz_harness_races_and_runners(meeting_url, meeting_date)


@st.cache_data(show_spinner=False)
def cached_nz_tb_meetings(d: date, refresh_nonce: int = 0) -> list:
    _ = refresh_nonce
    return fetch_nz_tb_meetings_for_date(d, ttl_seconds=30 * 60)


@st.cache_data(show_spinner=False)
def cached_nz_tb_fields(meeting_url: str, meeting_date: date) -> tuple[list, dict, dict]:
    """NZ thoroughbred: races + runners from nzracing.co.nz (races/runners may be empty if not yet parsed)."""
    races, runners_by_race, _ = fetch_nz_tb_races_and_runners(meeting_url, meeting_date)
    return (races, runners_by_race, {})


@st.cache_data(show_spinner=False)
def cached_sky_schedule(d: date) -> list[dict]:
    """Sky Racing 1/2 schedule for overlay (schedule.skyracing.com.au). Best-effort."""
    try:
        return fetch_sky_schedule(d, ttl_seconds=30 * 60)
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


@st.cache_data(show_spinner=False)
def cached_venue_weather(venue: str):
    return venue_weather(venue)

@st.cache_data(show_spinner=False)
def cached_race_weather(venue: str, meeting_date: date, start_time_local):
    return venue_weather_for_race(venue, meeting_date, start_time_local)


@st.dialog("Race roster (what's run / what's next)")
def race_roster_dialog(*, chosen_date: date, code_label: str, meetings: list, fields_by_meeting: dict, open_nonce: int = 0) -> None:
    # Widen this dialog (Streamlit dialogs are otherwise fairly narrow).
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
    now = datetime.now(tz).astimezone() if tz is not None else datetime.now().astimezone()
    now_str = now.strftime("%H:%M")

    # Default per-code race duration (used only when row_code not set); per-row we use per_race_m.
    per_race = timedelta(minutes=25 if code == "greyhound" else 35 if code == "thoroughbred" else 30)

    # Streamlit remembers widget state; force sane defaults each time the roster is opened
    # so the roster shows ALL races by default (all venues + finished included).
    filters_key = (code_label, chosen_date.isoformat(), int(open_nonce or 0))
    if st.session_state.get("roster_filters_key") != filters_key:
        st.session_state.roster_filters_key = filters_key
        st.session_state.roster_show_finished = False
        st.session_state.roster_only_next_per_venue = False
        st.session_state.roster_type_filter = "all"
        # Default best pick ON when opening roster so picks are computed; user can turn off.
        st.session_state.roster_show_best_pick = True
        st.session_state.roster_pick_limit = int(st.session_state.get("roster_pick_limit", 20))

    show_finished = st.toggle("Show finished races", key="roster_show_finished")

    # Type filter: All | Thoroughbred | Harness | Greyhound (only when roster has mixed types)
    if code_label in ("All (AU)", "All (AU+NZ)"):
        type_filter = st.session_state.get("roster_type_filter", "all")
        b_all, b_tb, b_hr, b_gh = st.columns(4)
        with b_all:
            if st.button("**All**", key="roster_btn_all", use_container_width=True):
                st.session_state.roster_type_filter = "all"
                st.rerun()
        with b_tb:
            if st.button("**Thoroughbred**", key="roster_btn_tb", use_container_width=True):
                st.session_state.roster_type_filter = "thoroughbred"
                st.rerun()
        with b_hr:
            if st.button("**Harness**", key="roster_btn_hr", use_container_width=True):
                st.session_state.roster_type_filter = "harness"
                st.rerun()
        with b_gh:
            if st.button("**Greyhound**", key="roster_btn_gh", use_container_width=True):
                st.session_state.roster_type_filter = "greyhound"
                st.rerun()
        st.caption(f"Showing: **{st.session_state.get('roster_type_filter', 'all').capitalize()}** only." if st.session_state.get("roster_type_filter") != "all" else "Showing: **All** (thoroughbred, harness, greyhound).")
    only_next_per_venue = st.toggle("Only show next upcoming per venue", key="roster_only_next_per_venue")
    st.caption(
        f"**Current time (app):** **{now_str}** ({tz_name}). "
        "Leave **'Only show next upcoming per venue'** OFF to see **every race** (all R1, R2, … at each venue) with ~5 min gaps between start times. "
        "Table times are from the data source (often venue local)."
    )
    if only_next_per_venue:
        st.caption("One row per venue: the **next** race at that venue (soonest first). Turn **OFF** to see every race at every venue with ~5 min gaps (e.g. 13:45, 13:50, 14:00). (In progress or soonest upcoming.)")
    else:
        st.caption("**All races** shown: every race at every venue. You should see many rows with ~5 min gaps between start times.")

    rows = []
    next_race = None  # (dt, row)
    used_approx = False

    # Optional: show best pick per race (best-effort; can be slow for greyhounds).
    show_best_pick = st.toggle("Show best pick (best-effort; can be slow)", value=st.session_state.get("roster_show_best_pick", True), key="roster_show_best_pick")
    pick_limit = st.slider(
        "Max picks to compute",
        0,
        50,
        value=int(st.session_state.get("roster_pick_limit", 20)),
        step=1,
        disabled=(not show_best_pick),
        key="roster_pick_limit",
    )
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

    def _runners_for_roster_row(r: dict) -> list:
        """Return list of (display_no, name, scratched) for the race in this roster row."""
        row_code = r.get("_code") or code
        race_link = str(r.get("race_link") or "")
        if row_code == "greyhound":
            # NZ greyhound: use pre-loaded runners (no GRNZ runner fetch); avoid 403 on grnz.co.nz URLs
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
        out = []
        for i, runner in enumerate(runners or []):
            name = getattr(runner, "name", "") or ""
            num = _runner_number_for_name(runners, name) or (
                str(getattr(runner, "draw", "")) if getattr(runner, "draw", None) is not None else str(i + 1)
            )
            scratched = bool(getattr(runner, "scratched", False))
            out.append((num, name, scratched))
        return out

    # Keep quick actions visible near the top (dialogs can be tall; users may not scroll).
    if "roster_selected" not in st.session_state:
        st.session_state.roster_selected = None
    top_b1, top_b2 = st.columns([0.35, 0.65], vertical_alignment="center")
    with top_b1:
        sel0 = st.session_state.roster_selected or {}
        cbtn2, cbtn3 = st.columns([0.5, 0.5], vertical_alignment="center")
        with cbtn2:
            st.link_button(
                "Open race",
                url=str(sel0.get("race_link") or ""),
                type="secondary",
                disabled=not bool(sel0 and sel0.get("race_link")),
            )
        with cbtn3:
            st.link_button(
                "Open meeting",
                url=str(sel0.get("meeting_link") or ""),
                type="secondary",
                disabled=not bool(sel0 and sel0.get("meeting_link")),
            )

    with top_b2:
        sel0 = st.session_state.roster_selected or {}
        if sel0 and sel0.get("venue") and sel0.get("race"):
            st.caption(f"Selected: **{sel0.get('venue')} {sel0.get('race')}** — {sel0.get('pick') or 'no pick computed'}")
        else:
            st.caption("Tip: use the inline WHY button on a row to see the rationale.")

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
        mf = fields_by_meeting.get(getattr(m, "meeting_url", ""), {}) or {}
        races = mf.get("races") or []
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
            type_display = _code_to_type.get((row_code or "").lower(), (row_code or "").capitalize() or "—")
            row = {
                "venue": venue_disp,
                "type": type_display,
                "race_no": getattr(r, "race_no", None),
                "race": f"R{getattr(r, 'race_no', '')}",
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

    num_venues = len(set((r.get("venue") or "") for _, r in rows))
    total_races = len(rows)
    st.caption(f"Loaded **{num_venues}** meeting(s), **{total_races}** race(s) for this date.")
    if num_venues <= 1 and total_races < 20:
        st.caption("Only one venue with races was found. If you expected more (e.g. afternoon meetings), the racecards page may only list evening meetings for this date, or the source may have returned limited data (e.g. 403). Try the **Meetings** button to see what was detected.")
    # Show this hint only when the soonest upcoming race is actually in the evening.
    if next_race is not None and isinstance(next_race[0], datetime) and next_race[0].hour >= 18:
        st.caption(
            "Only seeing evening times (~18:00+)? That's what the source returned for this date — **not a regression**. "
            "Turn **off** 'Only show next upcoming per venue' and **on** 'Show finished races' to see all races for the day."
        )

    if next_race is None:
        if code == "greyhound" and used_approx:
            st.caption("Next race: N/A (could not infer schedule for upcoming races).")
        else:
            st.caption("Next race: N/A (no upcoming races with a known start time).")
    else:
        dt, row = next_race
        mins = int((dt - now).total_seconds() // 60)
        approx_note = " (approx)" if (row.get("time") or "").startswith("~") else ""
        st.success(
            f"**Current time (app):** **{now_str}**. "
            f"**Soonest upcoming in list:** {row['venue']} {row['race']} at {row['time']} (in ~{mins} min){approx_note}. "
            "The table below shows **all races** loaded for this date — use toggles above to include finished races or one row per venue."
        )
        st.caption("If that time isn’t your local time (e.g. you’re in Australia), the app is using the server’s timezone. Run it locally for correct countdown.")

    if used_approx:
        st.caption("Note: times with a leading '~' are approximated from the meeting's first race time (greyhounds v0).")

    # filters (use session state so toggle choice is applied even if variable is stale)
    hide_finished = not st.session_state.get("roster_show_finished", False)
    if hide_finished:
        rows = [(dt, r) for (dt, r) in rows if (r.get("status") or "").strip().lower() != "finished"]

    if only_next_per_venue:
        # One row per venue: show the "next" race (in_progress or soonest upcoming)
        by_venue = {}
        for dt, r in rows:
            if dt is None:
                continue
            row_status = (r.get("status") or "").strip().lower()
            if row_status == "finished":
                continue
            v = r.get("venue") or ""
            if v not in by_venue:
                by_venue[v] = (dt, r)
            else:
                ex_dt, ex_r = by_venue[v]
                ex_st = (ex_r.get("status") or "").strip().lower()
                # Prefer in_progress (current race) over upcoming
                if row_status == "in_progress" and ex_st != "in_progress":
                    by_venue[v] = (dt, r)
                elif ex_st == "in_progress" and row_status != "in_progress":
                    pass
                elif dt < ex_dt:
                    by_venue[v] = (dt, r)
        rows = list(by_venue.values())

    # sort by time (unknown times last), then venue, then race
    def sort_key(item):
        dt, r = item
        return (
            dt is None,
            dt or datetime.max.replace(tzinfo=now.tzinfo),
            r.get("venue") or "",
            r.get("race") or "",
        )

    rows_sorted = [r for _, r in sorted(rows, key=sort_key)]

    # Apply type filter when in All mode (All | Thoroughbred | Harness | Greyhound)
    type_filter = st.session_state.get("roster_type_filter", "all")
    if code_label in ("All (AU)", "All (AU+NZ)") and type_filter != "all":
        rows_sorted = [r for r in rows_sorted if (r.get("_code") or "").lower() == type_filter]

    # Compute best picks *for the displayed rows* (so we don't waste the limit on hidden rows).
    if show_best_pick and pick_limit > 0 and rows_sorted:
        prog = st.progress(0)
        first_error = None
        with st.spinner("Computing best picks..."):
            for rr in rows_sorted:
                if computed_picks >= pick_limit:
                    break
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
                                rr["_best_pick_why"] = list(ranked[0].why_bullets)[:6]
                                rr["best_pick_no"] = _runner_number_for_name(runners, ranked[0].name)
                                if len(ranked) >= 2:
                                    rr["if_scratched"] = ranked[1].name
                                    rr["_backup_pick_why"] = list(ranked[1].why_bullets)[:6]
                                    rr["if_scratched_no"] = _runner_number_for_name(runners, ranked[1].name)
                                # Roughie = last-ranked (long-shot) pick
                                rr["roughie"] = ranked[-1].name
                                rr["roughie_no"] = _runner_number_for_name(runners, ranked[-1].name)
                                # Inline rationale summary for the row.
                                kf = getattr(ranked[0], "key_factors", "") or ""
                                if not kf:
                                    kf = "; ".join([b.strip("- ").strip() for b in rr["_best_pick_why"] if b])[:180]
                                rr["why"] = _truncate(kf, 110)
                                computed_picks += 1
                except Exception as e:
                    if first_error is None:
                        first_error = e
                prog.progress(int(min(computed_picks, pick_limit) / max(pick_limit, 1) * 100))
        prog.empty()
        st.caption(f"Computed {computed_picks} best picks (limit {pick_limit}).")
        if first_error is not None:
            st.warning(f"Some races failed to rank (first error: {first_error!s}). Check field data is loaded.")

    # Display mode:
    # - st.dataframe cannot render per-row buttons, so we provide an "interactive list" view
    #   with a real WHY button inline for each row.
    default_mode = "Interactive rows (WHY popup)" if len(rows_sorted) <= 150 else "Table (faster for large lists)"
    mode = st.radio(
        "Roster display",
        options=["Interactive rows (WHY popup)", "Pretty rows (full-row zebra; no popovers)", "Table (faster for large lists)"],
        index=0 if default_mode.startswith("Interactive") else 1,
        horizontal=True,
    )
    overlay_sky_roster = st.checkbox(
        "Overlay Sky 1/2 schedule (compare)",
        value=True,
        key="roster_overlay_sky",
        help="Match roster rows to Sky Racing 1/2 schedule; show sky_channel, sky_dt, delta_minutes.",
    )

    def _normalize_venue_sky_roster(v: str) -> str:
        s = (v or "").strip()
        s = re.sub(r"\s*\([^)]+\)\s*$", "", s).strip()
        return s.lower()

    if mode.startswith("Table"):
        display_rows = []
        for r in rows_sorted:
            pick = r.get("best_pick", "")
            pick_no = r.get("best_pick_no", "")
            pick_disp = (f"{pick_no}. {pick}" if pick_no and pick else pick)
            bkup = r.get("if_scratched", "")
            bkup_no = r.get("if_scratched_no", "")
            bkup_disp = (f"→ {bkup_no}. {bkup}" if bkup_no and bkup else (f"→ {bkup}" if bkup else ""))
            rough = r.get("roughie", "")
            rough_no = r.get("roughie_no", "")
            rough_disp = (f"{rough_no}. {rough}" if rough_no and rough else rough)
            fs = r.get("field_size")
            display_rows.append(
                {
                    "venue": r.get("venue", ""),
                    "type": r.get("type", ""),
                    "race": r.get("race", ""),
                    "time": r.get("time", ""),
                    "status": r.get("status", ""),
                    "field size": str(fs) if fs is not None else "",
                    "best_pick": pick_disp,
                    "if_scratched": bkup_disp,
                    "roughie": rough_disp,
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
            roster_cols = ["venue", "type", "race", "time", "status", "field size", "best_pick", "if_scratched", "roughie", "sky_channel", "sky_dt", "delta_minutes", "delta_note", "why"]
        else:
            for d in display_rows:
                d.pop("_r", None)
            roster_cols = ["venue", "type", "race", "time", "status", "field size", "best_pick", "if_scratched", "roughie", "why"]
        st.dataframe(
            display_rows,
            width="stretch",
            hide_index=True,
            column_order=roster_cols,
        )
        st.caption("Tip: switch to 'Interactive rows' if you want an inline WHY button per row.")
        return

    if mode.startswith("Pretty"):
        # Pretty HTML-only rendering: full-row zebra + aligned columns, but no Streamlit popovers.
        st.caption("Pretty view uses HTML expanders (not Streamlit popovers). Switch to Interactive for real WHY popups.")
        st.markdown(
            """
<style>
  .roster-row { display: flex; align-items: center; flex-wrap: nowrap; gap: 6px; margin: 2px 0; border-radius: 6px; padding: 7px 6px; min-height: 2.2em; box-sizing: border-box; }
  .roster-cell { flex-shrink: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .roster-cell a { text-decoration: underline; }
  .roster-cell details { font-size: 0.9em; }
  .roster-cell details summary { cursor: pointer; list-style: none; }
  .roster-cell details summary::-webkit-details-marker { display: none; }
  .roster-cell:has(details[open]) { overflow: visible; white-space: normal; }
  .roster-cell details[open] ul { white-space: normal; min-width: 12em; }
  .roster-field-ul { margin: 4px 0; padding-left: 1.2em; font-size: 0.9em; min-width: 11em; list-style: disc; line-height: 1.4; }
  .roster-field-li { white-space: normal; word-break: break-word; }
  .roster-cell-pick { white-space: normal !important; overflow-wrap: break-word; min-width: 7em; text-overflow: clip; }
  .roster-header { background: transparent !important; font-weight: 600; margin-bottom: 4px; }
</style>
""",
            unsafe_allow_html=True,
        )

        if overlay_sky_roster:
            sky_list_pretty = cached_sky_schedule(chosen_date)
            sky_by_key_pretty: dict[tuple[str, str, int], dict] = {}
            for s in sky_list_pretty:
                ven = (s.get("venue") or "").strip()
                rn = s.get("race_no")
                if rn is None:
                    continue
                try:
                    rn = int(rn)
                except (TypeError, ValueError):
                    continue
                key = ("AU", _normalize_venue_sky_roster(ven), rn)
                if key not in sky_by_key_pretty:
                    sky_by_key_pretty[key] = s
            col_widths = [0.028, 0.08, 0.04, 0.04, 0.05, 0.05, 0.04, 0.05, 0.09, 0.09, 0.06, 0.04, 0.04, 0.04, 0.04, 0.08, 0.05, 0.04, 0.04]
            hdr_labels = ["", "**venue**", "**type**", "**race**", "**time**", "**status**", "**field**", "**runners**", "**best pick**", "**if scratched**", "**roughie**", "**Sky**", "**sky time**", "**Δ min**", "**Δ note**", "**why (short)**", "**Open**", "**odds**", "**why**"]
        else:
            # Give best pick / if scratched / roughie width so names don't truncate
            col_widths = [0.028, 0.08, 0.05, 0.05, 0.05, 0.06, 0.04, 0.05, 0.12, 0.12, 0.10, 0.09, 0.06, 0.04, 0.04]
            hdr_labels = ["", "**venue**", "**type**", "**race**", "**time**", "**status**", "**field**", "**runners**", "**best pick**", "**if scratched**", "**roughie**", "**why (short)**", "**Open**", "**odds**", "**why**"]
        col_pct = [f"{w * 100:.1f}%" for w in col_widths]
        n_cols = len(col_widths)

        def cell_html(content: str, flex: str, allow_wrap: bool = False) -> str:
            cls = "roster-cell roster-cell-pick" if allow_wrap else "roster-cell"
            return f'<div class="{cls}" style="flex: 0 0 {flex};">{content}</div>'

        hdr_row = '<div class="roster-row roster-header">' + "".join(cell_html(hdr_labels[i], col_pct[i]) for i in range(n_cols)) + "</div>"
        st.markdown(hdr_row, unsafe_allow_html=True)

        for row_i, r in enumerate(rows_sorted):
            bar_bg = "rgba(255,255,255,0.12)" if (row_i % 2 == 0) else "rgba(255,255,255,0.04)"
            race_link = str(r.get("race_link") or "")
            pick = r.get("best_pick", "")
            pick_no = r.get("best_pick_no", "")
            pick_disp = f"{pick_no}. {pick}" if pick_no and pick else pick
            bkup = r.get("if_scratched", "")
            bkup_no = r.get("if_scratched_no", "")
            bkup_disp = f"→ {bkup_no}. {bkup}" if bkup_no and bkup else (f"→ {bkup}" if bkup else "")

            sky_ch, sky_dt, delta_min, delta_note = "", "—", "—", ""
            if overlay_sky_roster:
                country = (r.get("country") or "AU").strip()
                venue = (r.get("venue") or "").strip()
                rno = r.get("race_no")
                try:
                    rno_int = int(rno) if rno is not None else None
                except (TypeError, ValueError):
                    rno_int = None
                if rno_int is not None:
                    key1 = (country, _normalize_venue_sky_roster(venue), rno_int)
                    sky = sky_by_key_pretty.get(key1) or (sky_by_key_pretty.get(("AU", _normalize_venue_sky_roster(venue), rno_int)) if country == "AU" else None)
                    if sky:
                        sky_ch = str(sky.get("channel", ""))
                        dt_sky = sky.get("dt_app_tz") or sky.get("dt_local")
                        sky_dt = dt_sky.strftime("%H:%M") if dt_sky else "—"
                        our_dt = r.get("dt")
                        if our_dt is not None and dt_sky is not None:
                            dm = round((our_dt - dt_sky).total_seconds() / 60)
                            delta_min = str(dm)
                            delta_note = "⚠ ≥2 min" if abs(dm) >= 2 else ""

            fs_val = r.get("field_size")
            fs_str = str(fs_val) if fs_val is not None else ""
            c0 = cell_html("", col_pct[0])
            c1 = cell_html(html.escape(r.get("venue", "")), col_pct[1])
            c_type = cell_html(html.escape(r.get("type", "")), col_pct[2])
            c2 = cell_html(html.escape(r.get("race", "")), col_pct[3])
            c3 = cell_html(html.escape(r.get("time", "")), col_pct[4])
            c4 = cell_html(html.escape(r.get("status", "")), col_pct[5])
            c5_field = cell_html(html.escape(fs_str), col_pct[6])
            field_list = _runners_for_roster_row(r)
            best_name = (r.get("best_pick") or "").strip()
            backup_name = (r.get("if_scratched") or "").strip()
            runners_items = []
            for num, name, scratched in field_list:
                suffix = " (SCR)" if scratched else ""
                if name == best_name:
                    suffix = " ★\u00a0Pick" + suffix  # nbsp so "Pick" stays with ★
                elif name == backup_name:
                    suffix = " ★\u00a0Backup" + suffix
                runners_items.append(f"<li class='roster-field-li'>{html.escape(num)}. {html.escape(name)}{html.escape(suffix)}</li>")
            runners_details = (
                f"<details><summary>Field ▾</summary><ul class='roster-field-ul'>{''.join(runners_items)}</ul></details>"
                if field_list else "<span style='font-size:0.85em;'>—</span>"
            )
            c_runners = cell_html(runners_details, col_pct[7])
            c6 = cell_html(html.escape(pick_disp), col_pct[8], allow_wrap=True)
            c7_bkup = cell_html(html.escape(bkup_disp), col_pct[9], allow_wrap=True)
            rough_disp = (f"{r.get('roughie_no','')}. {r.get('roughie','')}" if r.get("roughie_no") and r.get("roughie") else r.get("roughie", ""))
            c_roughie = cell_html(html.escape(rough_disp), col_pct[10], allow_wrap=True)
            idx_why = 15 if overlay_sky_roster else 11
            idx_open = 16 if overlay_sky_roster else 12
            idx_odds = 17 if overlay_sky_roster else 13
            idx_why_details = 18 if overlay_sky_roster else 14
            if overlay_sky_roster:
                c7a = cell_html(html.escape(sky_ch), col_pct[11])
                c7b = cell_html(html.escape(sky_dt), col_pct[12])
                c7c = cell_html(html.escape(delta_min), col_pct[13])
                c7d = cell_html(html.escape(delta_note), col_pct[14])
            c7 = cell_html(html.escape(r.get("why", "")), col_pct[idx_why])
            open_a = f'<a href="{html.escape(race_link)}" target="_blank" rel="noopener">Open</a>' if race_link else "Open"
            c8 = cell_html(open_a, col_pct[idx_open])

            q = f"site:tab.com.au racing {chosen_date.isoformat()} {r.get('venue','')} {r.get('race','')}"
            search_url = f"https://www.google.com/search?q={quote(q)}"
            odds_details = (
                "<details><summary>Odds ▾</summary>"
                "<p style='margin:4px 0;font-size:0.85em;'>No live odds in app.</p>"
                '<a href="https://www.tab.com.au/racing" target="_blank" rel="noopener">Open TAB Racing</a><br>'
                f'<a href="{search_url}" target="_blank" rel="noopener">Search TAB for this race</a></details>'
            )
            c9 = cell_html(odds_details, col_pct[idx_odds])

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
            c10 = cell_html(why_details, col_pct[idx_why_details])

            row_html = (
                f'<div class="roster-row" style="background:{bar_bg};">'
                + (f"{c0}{c1}{c_type}{c2}{c3}{c4}{c5_field}{c_runners}{c6}{c7_bkup}{c_roughie}{c7a}{c7b}{c7c}{c7d}{c7}{c8}{c9}{c10}" if overlay_sky_roster else f"{c0}{c1}{c_type}{c2}{c3}{c4}{c5_field}{c_runners}{c6}{c7_bkup}{c_roughie}{c7}{c8}{c9}{c10}")
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
    if not has_any_pick and show_best_pick:
        st.caption(
            "No picks in table: runner fetch may be blocked (e.g. 403) or limit reached. "
            "Try increasing **Max picks to compute** or refresh later."
        )
    elif not has_any_pick:
        st.caption("Turn on **Show best pick** and set **Max picks to compute** to fill best pick / if scratched / why.")

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
    hdr[5].markdown("**status**")
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
        cols[5].write(r.get("status", ""))
        fs = r.get("field_size")
        cols[6].write(str(fs) if fs is not None else "")

        with cols[7]:
            field_list = _runners_for_roster_row(r)
            best_name = (r.get("best_pick") or "").strip()
            backup_name = (r.get("if_scratched") or "").strip()
            with st.popover("Field", help="Show all runners with box/draw numbers; ★ marks our picks"):
                st.caption(f"**{r.get('venue','')} {r.get('race','')}** — {len(field_list)} runner(s)")
                if field_list:
                    for num, name, scratched in field_list:
                        suffix = " (SCR)" if scratched else ""
                        if name == best_name:
                            suffix = " ★ Pick" + suffix
                        elif name == backup_name:
                            suffix = " ★ Backup" + suffix
                        st.markdown(f"{num}. **{name}**{suffix}")
                else:
                    st.caption("Field not loaded (use main page to load this meeting).")

        pick = r.get("best_pick", "")
        pick_no = r.get("best_pick_no", "")
        pick_text = f"{pick_no}. {pick}" if pick_no and pick else pick
        cols[8].markdown(f'<div class="roster-pick-cell">{html.escape(pick_text)}</div>', unsafe_allow_html=True)

        bkup = r.get("if_scratched", "")
        bkup_no = r.get("if_scratched_no", "")
        bkup_text = f"→ {bkup_no}. {bkup}" if bkup_no and bkup else (f"→ {bkup}" if bkup else "")
        cols[9].markdown(f'<div class="roster-pick-cell">{html.escape(bkup_text)}</div>', unsafe_allow_html=True)

        rough = r.get("roughie", "")
        rough_no = r.get("roughie_no", "")
        rough_text = f"{rough_no}. {rough}" if rough_no and rough else rough
        cols[10].markdown(f'<div class="roster-pick-cell">{html.escape(rough_text)}</div>', unsafe_allow_html=True)

        cols[11].write(r.get("why", ""))

        with cols[12]:
            st.link_button(
                "Open",
                url=str(r.get("race_link") or ""),
                type="secondary",
                disabled=not bool(r.get("race_link")),
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

@st.dialog("Daily review (winners vs our picks)")
def daily_review_dialog(chosen_date: date) -> None:
    st.write(f"**Date:** {chosen_date.isoformat()}")
    picks = load_picks(chosen_date)
    if not picks:
        st.info("No saved picks for this date yet. Rank a race, then click 'Save pick to Daily review'.")
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
        with st.spinner(f"Fetching winners for {venue} ({code})..."):
            results = fetch_results_for_meeting(code, meeting_url)

        race_nos = sorted({*results.keys(), *[rn for (mu, rn) in picks_by_meeting_race.keys() if mu == meeting_url and rn]})
        if not race_nos:
            return

        saved = len([1 for (mu, rn) in picks_by_meeting_race.keys() if mu == meeting_url and rn])
        with st.expander(f"{venue} — {code} — {meeting_date} ({saved} saved picks)", expanded=False):
            for rn in race_nos:
                p = picks_by_meeting_race.get((meeting_url, rn))
                winner = (results.get(rn).winner if results.get(rn) else None) or "N/A"
                pick_name = (p.get("pick_name") if p else None) or "—"
                hit = (winner != "N/A") and (pick_name != "—") and (winner.strip().lower() == pick_name.strip().lower())
                title = f"R{rn} — winner: {winner} — our pick: {pick_name}" + (" ✅" if hit else "")

                with st.expander(title, expanded=False):
                    st.write(f"**Meeting URL:** {meeting_url}")
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
    st.title("dog_rater_live")
    st.caption("For fun / exploratory only. Not betting advice. No odds, no paid APIs.")

    today = date.today()
    col_a, col_b = st.columns([1, 2], vertical_alignment="top")
    with col_a:
        st.write(f"**Today:** {today.isoformat()}")

    with col_b:
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

        if m is None:
            st.info("Next greyhound meeting: N/A (source blocked or no upcoming meetings found).")
        else:
            cd = countdown_to_meeting(m) or "time unknown"
            t = m.first_race_time_local.strftime("%H:%M") if m.first_race_time_local else "?"
            st.success(f"**Next upcoming greyhound meeting:** {m.venue} — first race {t} — {cd}")

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

    st.subheader("Controls")
    ctrl1, ctrl2, ctrl3 = st.columns([1.2, 1.3, 1.4], vertical_alignment="top")

    with ctrl1:
        code = st.selectbox(
            "Code",
            options=[
                "Greyhounds",
                "Thoroughbred (All AU)",
                "Harness (NSW)",
                "All (AU)",
                "Greyhounds (NZ)",
                "Harness (NZ)",
                "Thoroughbred (NZ)",
                "All (AU+NZ)",
            ],
            index=7,  # default: All (AU+NZ) — all of AU and NZ, all animals (races)
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
            st.caption("All AU meetings via Racing Australia FreeFields (best-effort scraping).")
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

    # --- Auto-load meetings for chosen date + code (helper supports single-code and All (AU)) ---
    if "meetings" not in st.session_state:
        st.session_state.meetings = []
    refresh_nonce = int(st.session_state.get("refresh_nonce", 0))
    if st.session_state.get("meetings_loaded_key") != (code, chosen_date, refresh_nonce):
        try:
            meetings = get_meetings_for_code(code, chosen_date, refresh_nonce)
            st.session_state.meetings = meetings
            st.session_state.meetings_loaded_key = (code, chosen_date, refresh_nonce)
        except (FetchError, DogsParseError, RacingAUSParseError, HarnessParseError, HrnzNzParseError, NzRacingParseError) as e:
            st.session_state.meetings = []
            st.session_state.meetings_loaded_key = (code, chosen_date, refresh_nonce)
            st.error(f"Could not load meetings: {e}")

    meetings = st.session_state.meetings

    with ctrl1:
        show_meetings = st.button("Show meetings for this date", disabled=(not meetings))
        if show_meetings:
            show_meetings_dialog(chosen_date, meetings, st.session_state.get("fields_by_meeting"))
        # Roster always shows All (AU+NZ) / all animals by default (load that set when opening).
        roster_code = "All (AU+NZ)"
        roster_meetings = get_meetings_for_code(roster_code, chosen_date, refresh_nonce)
        roster_ready = bool(roster_meetings)
        show_roster = st.button(
            "Race roster (what's run / what's next)",
            disabled=not roster_ready,
            help="Opens roster for all AU+NZ and all animals (greyhounds, thoroughbreds, harness).",
        )
        if show_roster and roster_ready:
            st.session_state.roster_open_nonce = int(st.session_state.get("roster_open_nonce", 0)) + 1
            # Load fields for roster meetings if not already loaded for All (AU+NZ)
            roster_fields_key = (roster_code, chosen_date, refresh_nonce)
            if st.session_state.get("fields_loaded_key") != roster_fields_key:
                roster_fb: dict = {}
                if roster_meetings:
                    with st.spinner("Loading roster (all AU+NZ, all animals)…"):
                        prog_roster = st.progress(0)
                        for i, m in enumerate(roster_meetings, start=1):
                            try:
                                m_code = getattr(m, "code", "") or ""
                                country = (getattr(m, "extra", {}) or {}).get("country") or "AU"
                                is_nz = country == "NZ" or getattr(m, "source", "") == "hrnz_nz"
                                is_nz_dog = country == "NZ" or getattr(m, "source", "") == "grnz_nz"
                                if m_code == "greyhound":
                                    if is_nz_dog:
                                        races, runners_by_race = cached_nz_dog_fields(m.meeting_url, m.meeting_date)
                                    else:
                                        races = cached_dog_races(m.meeting_url)
                                        runners_by_race = {}
                                        for race in races or []:
                                            rno = getattr(race, "race_no", None)
                                            rurl = getattr(race, "race_url", None) or ""
                                            if rno is not None and rurl:
                                                runners_by_race[rno] = cached_dog_runners(rurl)
                                    roster_fb[m.meeting_url] = {"races": races, "runners_by_race": runners_by_race, "meta": {}}
                                elif m_code == "thoroughbred":
                                    if is_nz:
                                        races, runners_by_race, meta = cached_nz_tb_fields(m.meeting_url, m.meeting_date)
                                    else:
                                        races, runners_by_race, meta = cached_tb_fields(m.meeting_url, refresh_nonce)
                                    roster_fb[m.meeting_url] = {"races": races, "runners_by_race": runners_by_race, "meta": meta}
                                elif m_code == "harness":
                                    if is_nz:
                                        races, runners_by_race = cached_nz_harness_fields(m.meeting_url, m.meeting_date)
                                    else:
                                        races, runners_by_race = cached_harness_fields(m.meeting_url, m.meeting_date)
                                    roster_fb[m.meeting_url] = {"races": races, "runners_by_race": runners_by_race, "meta": {}}
                                else:
                                    roster_fb[m.meeting_url] = {"races": [], "runners_by_race": {}, "meta": {}}
                            except Exception:
                                roster_fb[m.meeting_url] = {"races": [], "runners_by_race": {}, "meta": {}}
                            prog_roster.progress(int(i / max(len(roster_meetings), 1) * 100))
                        prog_roster.empty()
                roster_fields = roster_fb
            else:
                roster_fields = st.session_state.get("fields_by_meeting") or {}
            race_roster_dialog(
                chosen_date=chosen_date,
                code_label=roster_code,
                meetings=roster_meetings,
                fields_by_meeting=roster_fields,
                open_nonce=int(st.session_state.roster_open_nonce),
            )
        show_review = st.button("Daily review (winners vs our picks)")
        if show_review:
            daily_review_dialog(chosen_date)
        show_compression = st.button("Compression backtest (TB)")
        if show_compression:
            compression_backtest_dialog(chosen_date)

    # --- Auto-load races (and runners for TB/harness) for ALL venues; branch on m.code for mixed All (AU) ---
    if "fields_by_meeting" not in st.session_state:
        st.session_state.fields_by_meeting = {}
    if st.session_state.get("fields_loaded_key") != (code, chosen_date, refresh_nonce):
        fields_by_meeting: dict[str, dict] = {}
        if meetings:
            with st.spinner("Loading races for all venues (cached; 1 request/sec)..."):
                prog = st.progress(0)
                total = len(meetings)
                for i, m in enumerate(meetings, start=1):
                    try:
                        m_code = getattr(m, "code", "") or ""
                        country = (getattr(m, "extra", {}) or {}).get("country") or "AU"
                        is_nz = country == "NZ" or getattr(m, "source", "") == "hrnz_nz"
                        is_nz_dog = country == "NZ" or getattr(m, "source", "") == "grnz_nz"
                        if m_code == "greyhound":
                            if is_nz_dog:
                                races, runners_by_race = cached_nz_dog_fields(m.meeting_url, m.meeting_date)
                                fields_by_meeting[m.meeting_url] = {"races": races, "runners_by_race": runners_by_race, "meta": {}}
                            else:
                                races = cached_dog_races(m.meeting_url)
                                fields_by_meeting[m.meeting_url] = {"races": races, "runners_by_race": None, "meta": {}}
                        elif m_code == "thoroughbred":
                            if is_nz:
                                races, runners_by_race, meta = cached_nz_tb_fields(m.meeting_url, m.meeting_date)
                            else:
                                races, runners_by_race, meta = cached_tb_fields(m.meeting_url, refresh_nonce)
                            fields_by_meeting[m.meeting_url] = {"races": races, "runners_by_race": runners_by_race, "meta": meta}
                        elif m_code == "harness":
                            if is_nz:
                                races, runners_by_race = cached_nz_harness_fields(m.meeting_url, m.meeting_date)
                            else:
                                races, runners_by_race = cached_harness_fields(m.meeting_url, m.meeting_date)
                            fields_by_meeting[m.meeting_url] = {"races": races, "runners_by_race": runners_by_race, "meta": {}}
                        else:
                            fields_by_meeting[m.meeting_url] = {"races": [], "runners_by_race": {}, "meta": {}}
                    except Exception as e:
                        fields_by_meeting[m.meeting_url] = {"races": [], "runners_by_race": {}, "meta": {}}
                        st.warning(f"Could not load races for {m.venue}: {e}")
                    prog.progress(int(i / max(total, 1) * 100))
                prog.empty()
        st.session_state.fields_by_meeting = fields_by_meeting
        st.session_state.fields_loaded_key = (code, chosen_date, refresh_nonce)

    fields_by_meeting = st.session_state.fields_by_meeting

    # --- All (AU) or All (AU+NZ): unified Next-to-Jump grid ---
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
                    "class": "",  # best-effort if available from r.extra
                    "status": status_au,
                    "url": str(getattr(r, "race_url", "") or ""),
                    "meeting_url": getattr(m, "meeting_url", ""),
                    "time_disp": time_str,
                })
        # Sort by dt ascending (unknown times last).
        unified_rows.sort(key=lambda x: (x["dt"] is None, x["dt"] or datetime.max.replace(tzinfo=now_au.tzinfo)))
        next_to_jump = None
        for row in unified_rows:
            if row["dt"] is not None and row["dt"] >= now_au:
                next_to_jump = row
                break
        if next_to_jump:
            t = next_to_jump["time_disp"]
            rno = next_to_jump.get("race_no") or "?"
            ctry = next_to_jump.get("country") or "AU"
            st.success(
                f"**Next to jump:** {ctry} {next_to_jump['code']} — {next_to_jump['venue']} R{rno} at {t} — "
                f"**current time (app):** {now_au.strftime('%H:%M')} ({tz_name_au})"
            )
        else:
            st.info("Next to jump: N/A (no upcoming races with known times).")
        show_best_au = st.toggle("Show best pick (slow)", value=False, key="all_au_show_best_pick")
        overlay_sky = st.checkbox(
            "Overlay Sky 1/2 schedule (compare)",
            value=True,
            key="all_au_overlay_sky",
            help="Match grid rows to Sky Racing 1/2 schedule; show sky_channel, sky_dt, delta_minutes.",
        )
        # Build display rows for dataframe (time, country, code, venue, race, distance, name, status, url; optional top_pick, why; optional sky columns).
        display_au = []
        for row in unified_rows:
            rno = row.get("race_no") or ""
            dist = row.get("distance")
            # Coerce to str so st.dataframe/Arrow does not get mixed int/str in distance column
            distance_str = str(dist) if dist not in (None, "") else ""
            display_au.append({
                "time": str(row.get("time_disp") or ""),
                "country": str(row.get("country") or ""),
                "code": str(row.get("code") or ""),
                "venue": str(row.get("venue") or ""),
                "race": f"R{rno}",
                "distance": distance_str,
                "name": str((row.get("name") or "")[:40]),
                "status": str(row.get("status") or ""),
                "url": str(row.get("url") or ""),
                "_row": row,
            })
        if show_best_au:
            pick_limit_au = 30
            with st.spinner("Computing best picks for upcoming races (slow)..."):
                prog_au = st.progress(0)
                computed_au = 0
                for i, d in enumerate(display_au):
                    if computed_au >= pick_limit_au:
                        break
                    row = d["_row"]
                    if row.get("status") != "upcoming":
                        continue
                    try:
                        m_code = row["code"]
                        runners_au = []
                        if m_code == "greyhound":
                            runners_au = cached_dog_runners(str(row.get("url") or ""))
                        else:
                            mf_au = fields_by_meeting.get(str(row.get("meeting_url") or ""), {}) or {}
                            runners_by = mf_au.get("runners_by_race") or {}
                            runners_au = runners_by.get(row.get("race_no"), []) or []
                        runners_au = [x for x in runners_au if not bool(getattr(x, "scratched", False))]
                        if runners_au:
                            bw_au, fw_au, ew_au, _ = suggest_auto_weights(runners_au, weather=None, track_condition=None)
                            ranked_au = rank_runners(
                                runners_au, box_weight=bw_au, form_weight=fw_au, early_weight=ew_au,
                                weather=None, track_condition=None, explain_mode="short",
                            )
                            if ranked_au:
                                top = ranked_au[0]
                                d["top_pick"] = top.name
                                why_bullets = list(getattr(top, "why_bullets", []) or [])[:6]
                                d["why"] = "; ".join(b.strip("- ").strip() for b in why_bullets if b)[:120]
                                computed_au += 1
                    except Exception:
                        pass
                    prog_au.progress(min(100, int((i + 1) / max(len(display_au), 1) * 100)))
                prog_au.empty()
            st.caption(f"Computed best pick for up to {computed_au} upcoming races.")
        # Sky overlay: match rows to Sky schedule (country, venue_normalized, race_no); add sky_channel, sky_dt, delta_minutes
        def _normalize_venue_sky(v: str) -> str:
            s = (v or "").strip()
            s = re.sub(r"\s*\([^)]+\)\s*$", "", s).strip()
            return s.lower()

        if overlay_sky:
            sky_list_au = cached_sky_schedule(chosen_date)
            # Key: (country, venue_normalized, race_no). Sky schedule is AU only.
            sky_by_key: dict[tuple[str, str, int], dict] = {}
            for s in sky_list_au:
                ven = (s.get("venue") or "").strip()
                rn = s.get("race_no")
                if rn is None:
                    continue
                try:
                    rn = int(rn)
                except (TypeError, ValueError):
                    continue
                key = ("AU", _normalize_venue_sky(ven), rn)
                if key not in sky_by_key:
                    sky_by_key[key] = s
            for d in display_au:
                row = d.get("_row") or {}
                country = (row.get("country") or "AU").strip()
                venue = (row.get("venue") or "").strip()
                rno = row.get("race_no")
                try:
                    rno_int = int(rno) if rno is not None else None
                except (TypeError, ValueError):
                    rno_int = None
                d.setdefault("sky_channel", "")
                d.setdefault("sky_dt", "")
                d.setdefault("delta_minutes", "")
                d.setdefault("delta_note", "")
                if rno_int is not None:
                    key1 = (country, _normalize_venue_sky(venue), rno_int)
                    sky = sky_by_key.get(key1)
                    if sky is None and country == "AU":
                        key2 = ("AU", _normalize_venue_sky(venue), rno_int)
                        sky = sky_by_key.get(key2)
                    if sky:
                        d["sky_channel"] = str(sky.get("channel", ""))
                        dt_sky = sky.get("dt_app_tz") or sky.get("dt_local")
                        d["sky_dt"] = dt_sky.strftime("%H:%M") if dt_sky else "—"
                        our_dt = row.get("dt")
                        delta_min = None
                        if our_dt is not None and dt_sky is not None:
                            delta_min = round((our_dt - dt_sky).total_seconds() / 60)
                        d["delta_minutes"] = str(delta_min) if delta_min is not None else "—"
                        d["delta_note"] = "⚠ ≥2 min" if delta_min is not None and abs(delta_min) >= 2 else ""
        # Columns for display: drop _row; include top_pick, why only if show_best_au; include sky columns if overlay_sky
        cols_au = ["time", "country", "code", "venue", "race", "distance", "name", "status", "url"]
        if show_best_au:
            cols_au = ["time", "country", "code", "venue", "race", "top_pick", "why", "status", "url"]
            for d in display_au:
                d.setdefault("top_pick", "")
                d.setdefault("why", "")
        if overlay_sky:
            cols_au = [c for c in cols_au if c != "url"] + ["sky_channel", "sky_dt", "delta_minutes", "delta_note", "url"]
            for d in display_au:
                d.setdefault("sky_channel", "")
                d.setdefault("sky_dt", "")
                d.setdefault("delta_minutes", "")
                d.setdefault("delta_note", "")
        for d in display_au:
            d.pop("_row", None)
        st.dataframe(display_au, width="stretch", hide_index=True, column_order=cols_au)
        st.divider()

    # --- Single-code: Next race banner + venue/race selector + rank flow ---
    if code not in ("All (AU)", "All (AU+NZ)"):
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

