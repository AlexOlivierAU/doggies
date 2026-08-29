"""Settings, diagnostics, and access to non-thoroughbred tools."""

from __future__ import annotations

from datetime import date
from typing import Any, Callable, Optional

import streamlit as st

from race_db import backfill_jockey_rides, db_status, jockey_stats
from ui.components import inject_base_css

CODE_OPTIONS = [
    "Thoroughbred (All AU)",
    "Thoroughbred (AU + NZ)",
    "Thoroughbred (NZ)",
    "Greyhounds",
    "Greyhounds (NZ)",
    "Harness (NSW)",
    "Harness (NZ)",
    "All (AU)",
    "All (AU+NZ)",
]


def render_settings(
    *,
    chosen_date: date,
    render_full_roster: Optional[Callable[..., Any]] = None,
    meetings: Optional[list] = None,
    fields_by_meeting: Optional[dict] = None,
    code_label: str = "Thoroughbred (All AU)",
) -> None:
    inject_base_css()
    st.title("Settings")
    st.caption("Thoroughbred is the default. Greyhound and harness stay available here so Race Day stays uncluttered.")

    st.subheader("Timezone & refresh")
    st.selectbox("Timezone", options=["Australia/Sydney", "Pacific/Auckland", "Local (server)"], key="tz_name")
    st.number_input("Auto-refresh interval (seconds)", min_value=15, max_value=300, step=15, key="refresh_interval_sec")
    st.caption("Auto-refresh on Race Day updates the clock. The Refresh button reloads meetings and fields.")

    st.subheader("Data sources")
    st.selectbox(
        "Code (used when loading meetings)",
        options=CODE_OPTIONS,
        key="code_label",
        help="Race Day still filters to thoroughbreds. Other codes are for the full roster and Model/legacy tools.",
    )
    st.checkbox(
        "Show greyhound & harness on the full roster",
        key="include_other_codes",
        help="Does not add those codes to the Race Day hero/upcoming tables.",
    )

    st.subheader("Database")
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
    if st.button("Backfill jockey rides"):
        with st.spinner("Backfilling jockey rides..."):
            bf = backfill_jockey_rides()
        st.success(f"Backfilled **{bf.get('rides', 0)}** rides across **{bf.get('meetings', 0)}** meeting result sets.")

    with st.expander("Jockey / driver leaderboard"):
        scope = st.radio("Scope", ["This date", "Last 7 days", "All time"], horizontal=True, key="set_jockey_scope")
        min_rides = st.slider("Min rides", 1, 20, 3, key="set_jockey_min")
        date_from = date_to = None
        if scope == "This date":
            date_from = date_to = chosen_date
        elif scope == "Last 7 days":
            from datetime import timedelta

            date_from = chosen_date - timedelta(days=6)
            date_to = chosen_date
        stats = jockey_stats(code="thoroughbred", date_from=date_from, date_to=date_to, min_rides=int(min_rides), limit=40)
        if stats:
            st.dataframe(stats, width="stretch", hide_index=True)
        else:
            st.caption("No jockey rides stored yet.")

    st.subheader("Diagnostics")
    st.caption(
        "Public sources: Racing Australia (TB), Sportsbet public racing API (odds/scratchings, best-effort), "
        "thedogs / harness.org.au / HRNZ when those codes are loaded. Parsers can miss late changes."
    )
    if st.button("Force reload meetings/fields"):
        st.session_state.refresh_nonce = int(st.session_state.get("refresh_nonce", 0)) + 1
        st.session_state.roster_picks_cache = {}
        st.rerun()

    if render_full_roster and meetings is not None:
        with st.expander("Full roster grid (legacy / all codes)", expanded=False):
            st.caption("Original next-to-jump grid, including greyhound/harness when the selected code includes them.")
            render_full_roster(
                chosen_date=chosen_date,
                code_label=st.session_state.get("code_label") or code_label,
                meetings=meetings,
                fields_by_meeting=fields_by_meeting or {},
                open_nonce=0,
            )
