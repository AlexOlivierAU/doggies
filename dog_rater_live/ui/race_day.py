"""Race Day — default thoroughbred dashboard."""

from __future__ import annotations

import html as html_lib
from datetime import date, datetime
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from race_db import load_picks as db_load_picks
from services.pick_service import apply_view_scratching, maybe_autolock, save_selection_snapshot, snapshot_field
from services.race_day_service import (
    AU_STATES,
    RaceView,
    build_race_views,
    next_to_jump,
    upcoming_races,
    urgency_color,
)
from services.result_service import (
    daily_summary,
    resolve_pick_result,
    sync_missing_results,
)
from services.formatting import format_saved_selection
from ui.components import data_status_chip, inject_base_css, pick_cell, pick_metric, status_badge


def _open_race(view: RaceView) -> None:
    st.session_state.selected_race = {
        "meeting_url": view.meeting_url,
        "race_no": view.race_no,
        "venue": view.venue,
        "race_url": view.race_url,
    }
    st.session_state.nav_page = "Race Details"
    st.rerun()


def _save_and_lock(view: RaceView, chosen_date: date, db_path=None, *, lock: bool = True) -> None:
    from services.pick_service import build_snapshot_payload

    ranked_by_name = {getattr(r, "name", ""): r for r in (view.ranked or [])}
    payload = build_snapshot_payload(
        meeting_date=chosen_date,
        code=view.code,
        venue=view.venue_raw or view.venue,
        meeting_url=view.meeting_url,
        race_no=view.race_no,
        race_name=view.race_name or f"R{view.race_no}",
        race_url=view.race_url,
        primary=view.primary,
        backup=view.backup,
        primary_score=view.primary_score,
        backup_score=view.backup_score,
        key_factors="",
        why_bullets=view.why,
        weights=view.weights,
        field=snapshot_field(view.runners, ranked_by_name),
        primary_odds=view.odds,
        backup_odds=view.backup_odds,
        primary_number=view.primary_no,
        backup_number=view.backup_no,
        scheduled_jump=view.jump_at.isoformat() if view.jump_at else "",
        track_condition=view.track_condition,
        status=view.status,
        field_size=view.field_size,
    )
    kw = {"db_path": db_path} if db_path is not None else {}
    save_selection_snapshot(
        meeting_date=chosen_date,
        meeting_url=view.meeting_url,
        code=view.code,
        race_no=view.race_no,
        venue=view.venue_raw or view.venue,
        race_label=f"R{view.race_no}",
        primary=view.primary,
        backup=view.backup,
        pick_data=payload,
        best_score=view.primary_score,
        backup_score=view.backup_score,
        field_size=view.field_size,
        status=view.status,
        confidence_label=view.confidence_label,
        score_gap=view.score_gap,
        primary_odds=view.odds,
        backup_odds=view.backup_odds,
        scheduled_jump=view.jump_at.isoformat() if view.jump_at else "",
        lock=lock,
        **kw,
    )


def _save_live_unlocked(view: RaceView, chosen_date: date, db_path=None) -> None:
    _save_and_lock(view, chosen_date, db_path=db_path, lock=False)


def render_race_day(
    *,
    chosen_date: date,
    meetings: list,
    fields_by_meeting: dict,
    tz_name: str,
    now: datetime,
    app_tz: ZoneInfo,
    saved_picks: list[dict],
    results_by_key: dict,
    data_status: str,
    last_ok: Optional[str],
    odds_lookup: Optional[Callable[..., Any]] = None,
    sync_fetch=None,
    load_results_fn=None,
    persist_results_fn=None,
    persist_failure_fn=None,
    get_pick_fn=None,
) -> date:
    inject_base_css()
    st.title("Race Day")
    st.caption("Thoroughbreds first. Heuristic model picks — not betting advice.")

    bar = st.columns([1.1, 0.9, 0.7, 0.7, 1.4])
    with bar[0]:
        chosen_date = st.date_input("Date", key="chosen_date")
    with bar[1]:
        state_filter = st.selectbox("State", options=["All", *AU_STATES], key="state_filter")
    with bar[2]:
        st.toggle("Auto-refresh", key="auto_refresh", help="Updates the countdown clock. Use Refresh to reload fields.")
    with bar[3]:
        if st.button("Refresh", type="primary"):
            st.session_state.refresh_nonce = int(st.session_state.get("refresh_nonce", 0)) + 1
            st.session_state.pop("results_synced_key", None)
            st.rerun()
    with bar[4]:
        data_status_chip(data_status, last_ok)

    if st.session_state.get("auto_refresh"):
        interval = int(st.session_state.get("refresh_interval_sec") or 60)
        try:

            @st.fragment(run_every=interval)
            def _clock() -> None:
                st.caption(f"Clock {datetime.now(app_tz).strftime('%H:%M:%S')} · auto-refresh {interval}s")

            _clock()
        except Exception:
            st.caption(f"Clock {now.strftime('%H:%M:%S')}")

    picks_index = {}
    for p in saved_picks or []:
        try:
            picks_index[(p.get("meeting_url", ""), int(p.get("race_no") or 0))] = p
        except Exception:
            continue

    views = build_race_views(
        chosen_date=chosen_date,
        meetings=meetings,
        fields_by_meeting=fields_by_meeting,
        now=now,
        app_tz=app_tz,
        state_filter=str(state_filter),
        saved_picks=picks_index,
        odds_lookup=odds_lookup,
        rank_upcoming_only=False,
        upcoming_rank_limit=16,
    )

    for v in views:
        if not v.primary or getattr(v, "no_active_selection", False):
            continue
        if v.from_snapshot and v.locked:
            continue
        if v.status not in {"upcoming", "in_progress"}:
            continue
        _save_live_unlocked(v, chosen_date)

    try:
        reloaded = db_load_picks(chosen_date)
        picks_index = {}
        for p in reloaded or []:
            if (p.get("code") or "thoroughbred") != "thoroughbred":
                continue
            try:
                picks_index[(p.get("meeting_url", ""), int(p.get("race_no") or 0))] = p
            except Exception:
                continue
    except Exception:
        pass

    if get_pick_fn:
        for v in views:
            saved = picks_index.get(v.race_key)
            if not saved:
                continue
            updated = maybe_autolock(saved, now=now, jump_at=v.jump_at)
            picks_index[v.race_key] = updated
            if v.primary_scratched or v.backup_scratched or v.no_active_selection:
                scratched = apply_view_scratching(v, chosen_date=chosen_date, now=now)
                if scratched:
                    picks_index[v.race_key] = scratched

    if sync_fetch and load_results_fn and persist_results_fn and persist_failure_fn:
        sync_key = (chosen_date.isoformat(), int(st.session_state.get("refresh_nonce", 0)), "results_v1")
        if st.session_state.get("results_synced_key") != sync_key:
            st.session_state.results_synced_key = sync_key
            try:
                sync_missing_results(
                    chosen_date=chosen_date,
                    views=views,
                    now=now,
                    fetch_meeting_results=sync_fetch,
                    load_results_fn=load_results_fn,
                    persist_results_fn=persist_results_fn,
                    persist_failure_fn=persist_failure_fn,
                )
            except Exception:
                pass

    hero = next_to_jump(views, now)
    if hero is None:
        st.info("No upcoming thoroughbred race with a known jump time for this date/state.")
    else:
        _render_hero(hero, now, chosen_date)

    st.subheader("Upcoming")
    upcoming = upcoming_races(views, now, limit=8)
    if not upcoming:
        st.caption("No further upcoming races.")
    else:
        _render_upcoming_table(upcoming, now)

    st.subheader("Today's picks")
    _render_todays_picks(views, picks_index, results_by_key, now)
    return chosen_date


def _render_hero(hero: RaceView, now: datetime, chosen_date: date) -> None:
    dist = f"{hero.distance_m}m" if hero.distance_m else "—"
    warn = " · **Scratching warning**" if hero.scratching_warning else ""
    lock_note = "Saved snapshot" if hero.from_snapshot else "Live model pick"
    st.markdown(
        f"""
<div class="rd-hero">
  <div class="rd-kicker">Next to jump · {lock_note}{warn}</div>
  <h2 style="margin:0.2rem 0 0.4rem 0;">{hero.venue} R{hero.race_no}</h2>
  <div>{hero.clock()} · {hero.countdown(now)} · {dist} · {hero.race_class or "—"} · {hero.track_condition or "—"}</div>
</div>
""",
        unsafe_allow_html=True,
    )
    c1, c2, c3 = st.columns(3)
    with c1:
        if hero.no_active_selection:
            pick_metric("Primary", "NO ACTIVE SELECTION")
        elif hero.locked and hero.primary_scratched:
            orig = pick_cell(hero.original_primary_no, hero.original_primary)
            pick_metric("Original primary", f"{orig} — SCRATCHED")
        else:
            pick_metric("Primary", pick_cell(hero.primary_no, hero.primary, hero.odds))
    with c2:
        if hero.locked and hero.primary_scratched:
            pick_metric("Promoted primary", pick_cell(hero.primary_no, hero.primary, hero.odds) if hero.primary else "NO ACTIVE SELECTION")
        else:
            pick_metric("Backup", pick_cell(hero.backup_no, hero.backup, hero.backup_odds))
    with c3:
        st.metric("Confidence", hero.confidence_label or "—")
        st.caption("Label from score gap, not a probability.")
        if hero.selection_warning:
            st.caption(hero.selection_warning)
    b1, b2, b3 = st.columns([1, 1, 2])
    with b1:
        if st.button("Open race", key="hero_open"):
            _open_race(hero)
    with b2:
        if st.button("Confirm / save pick", key="hero_lock", disabled=not hero.primary or hero.no_active_selection):
            _save_and_lock(hero, chosen_date)
            st.success("Pick locked as a snapshot.")
            st.rerun()


def _render_upcoming_table(rows: list[RaceView], now: datetime) -> None:
    records = []
    for r in rows:
        token = urgency_color(r, now)
        records.append(
            {
                "Jump": f"{r.clock()} ({r.countdown(now)})",
                "Venue": r.venue,
                "Race": f"R{r.race_no}",
                "Primary": pick_cell(r.primary_no, r.primary, r.odds),
                "Backup": pick_cell(r.backup_no, r.backup, r.backup_odds),
                "Confidence": r.confidence_label,
                "Status": r.status,
                "_token": token,
                "_meeting_url": r.meeting_url,
                "_race_no": r.race_no,
            }
        )
    df = pd.DataFrame(records)
    show = df.drop(columns=["_token", "_meeting_url", "_race_no"])
    selected = []
    try:
        event = st.dataframe(
            show,
            width="stretch",
            hide_index=True,
            on_select="rerun",
            selection_mode="single-row",
            key="upcoming_table",
        )
        selected = list(event.selection.rows or [])
    except TypeError:
        st.dataframe(show, width="stretch", hide_index=True)
    if selected:
        i = selected[0]
        row = records[i]
        match = next((r for r in rows if r.meeting_url == row["_meeting_url"] and r.race_no == row["_race_no"]), None)
        if match and st.button(f"Open {match.venue} R{match.race_no}", key="open_upcoming"):
            _open_race(match)
    amber = [r for r in rows if urgency_color(r, now) == "amber"]
    red = [r for r in rows if urgency_color(r, now) == "red"]
    if red:
        st.warning("Scratching or urgent problem: " + ", ".join(f"{r.venue} R{r.race_no}" for r in red))
    if amber:
        st.caption("Amber: less than five minutes to jump — " + ", ".join(f"{r.venue} R{r.race_no}" for r in amber))


def _render_todays_picks(views: list[RaceView], picks_index: dict, results_by_key: dict, now: datetime) -> None:
    if not picks_index:
        st.caption("No saved picks yet. Confirm a pick or wait for auto-save from the live card.")
        return

    view_by_key = {v.race_key: v for v in views}
    resolved_rows = []
    table = []
    for key, pick in sorted(picks_index.items(), key=lambda kv: (kv[1].get("scheduled_jump") or "", kv[1].get("venue") or "", kv[0][1])):
        view = view_by_key.get(key)
        jump_at = view.jump_at if view else None
        result = results_by_key.get(key) or results_by_key.get((key[0], int(key[1]))) or {}
        resolved = resolve_pick_result(pick, result, now=now, jump_at=jump_at)
        row = {
            **pick,
            **resolved.as_dict(),
            "primary_odds": pick.get("primary_odds"),
            "confidence_label": pick.get("confidence_label") or (view.confidence_label if view else ""),
        }
        resolved_rows.append(row)
        venue = pick.get("venue") or (view.venue if view else "")
        race_no = pick.get("race_no")
        clock = view.clock() if view else (str(pick.get("scheduled_jump") or "")[11:16] or "—")
        table.append(
            {
                "Result": resolved.status,
                "Time": clock,
                "Race": f"{venue} R{race_no}",
                "Primary": format_saved_selection(pick, "primary"),
                "Primary pos": resolved.primary_finish_label,
                "Saved odds": pick.get("primary_odds") if pick.get("primary_odds") is not None else "—",
                "Backup": format_saved_selection(pick, "backup"),
                "Backup pos": resolved.backup_finish_label,
                "Source": resolved.result_source or resolved.match_note,
            }
        )

    summary = daily_summary(resolved_rows)
    m = st.columns(6)
    m[0].metric("Completed", summary.completed)
    m[1].metric("Primary wins", summary.primary_wins)
    m[2].metric("Primary places", summary.primary_places)
    m[3].metric("Backup wins", summary.backup_wins)
    m[4].metric("Win SR", f"{summary.win_strike_rate:.0%}" if summary.win_strike_rate is not None else "—")
    m[5].metric("Place SR", f"{summary.place_strike_rate:.0%}" if summary.place_strike_rate is not None else "—")
    if summary.estimated_win_return is not None:
        st.caption(f"{summary.estimated_return_label}: **{summary.estimated_win_return:+.2f}u**")
    else:
        st.caption("Financial return omitted — saved odds are incomplete.")

    if table:
        html_rows = []
        for rec in table:
            html_rows.append(
                "<tr>"
                f"<td>{status_badge(rec['Result'])}</td>"
                f"<td>{html_lib.escape(str(rec['Time']))}</td>"
                f"<td>{html_lib.escape(str(rec['Race']))}</td>"
                f"<td>{html_lib.escape(str(rec['Primary']))}</td>"
                f"<td>{html_lib.escape(str(rec['Primary pos']))}</td>"
                f"<td>{html_lib.escape(str(rec['Saved odds']))}</td>"
                f"<td>{html_lib.escape(str(rec['Backup']))}</td>"
                f"<td>{html_lib.escape(str(rec['Backup pos']))}</td>"
                f"<td class='rd-muted'>{html_lib.escape(str(rec['Source']))}</td>"
                "</tr>"
            )
        st.markdown(
            "<table width='100%'>"
            "<thead><tr><th>Result</th><th>Time</th><th>Race</th><th>Primary</th>"
            "<th>Pos</th><th>Odds</th><th>Backup</th><th>Pos</th><th>Source</th></tr></thead>"
            f"<tbody>{''.join(html_rows)}</tbody></table>",
            unsafe_allow_html=True,
        )
