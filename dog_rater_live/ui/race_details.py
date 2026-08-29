"""Per-race analysis from the loaded card + heuristic ranking."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

import streamlit as st

from parse_racingaustralia import runner_class_arrow, runner_last_class
from services.race_day_service import RaceView, build_race_views, number_for_name
from services.ranking import rank_field
from ui.components import inject_base_css, pick_cell


def render_race_details(
    *,
    chosen_date: date,
    meetings: list,
    fields_by_meeting: dict,
    now: datetime,
    app_tz: ZoneInfo,
    saved_picks: list[dict],
    odds_lookup: Optional[Callable[..., Any]] = None,
) -> None:
    inject_base_css()
    st.title("Race Details")
    st.caption("Compact field view. Score components stay collapsed until you expand a runner.")

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
        state_filter=str(st.session_state.get("state_filter") or "All"),
        saved_picks=picks_index,
        odds_lookup=odds_lookup,
    )
    if not views:
        st.info("No thoroughbred races loaded. Open Race Day and refresh.")
        return

    selected = st.session_state.get("selected_race") or {}
    labels = [f"{v.venue} R{v.race_no} · {v.clock()}" for v in views]
    default_idx = 0
    if selected:
        for i, v in enumerate(views):
            if v.meeting_url == selected.get("meeting_url") and v.race_no == selected.get("race_no"):
                default_idx = i
                break
    choice = st.selectbox("Race", options=list(range(len(views))), format_func=lambda i: labels[i], index=default_idx)
    view: RaceView = views[int(choice)]
    st.session_state.selected_race = {
        "meeting_url": view.meeting_url,
        "race_no": view.race_no,
        "venue": view.venue,
        "race_url": view.race_url,
    }

    dist = f"{view.distance_m}m" if view.distance_m else "—"
    st.markdown(
        f"**{view.venue} R{view.race_no}** · {view.clock()} · {dist} · {view.race_class or '—'} · "
        f"{view.track_condition or '—'} · field {view.field_size}"
    )
    if view.race_url:
        st.caption(view.race_url)
    if view.from_snapshot:
        st.info("Primary/backup below are the **saved snapshot**, not a live re-rank of an old race.")
    c1, c2, c3 = st.columns(3)
    c1.metric("Primary", pick_cell(view.primary_no, view.primary, view.odds))
    c2.metric("Backup", pick_cell(view.backup_no, view.backup, view.backup_odds))
    c3.metric("Confidence", view.confidence_label or "—")

    ranked, weights, rationale = rank_field(view.runners, track_condition=view.meta.get("track_condition"))
    runner_by_name = {getattr(r, "name", ""): r for r in view.runners}

    st.caption(
        f"Adaptive heuristic weights — draw {weights[0]:.2f} · form {weights[1]:.2f} · "
        f"class/weight {weights[2]:.2f}. Not trained AI."
    )

    for rr in ranked:
        runner = runner_by_name.get(rr.name)
        no = number_for_name(view.runners, rr.name)
        mark = ""
        if rr.name == view.primary:
            mark = " · PRIMARY"
        elif rr.name == view.backup:
            mark = " · BACKUP"
        scratched = bool(getattr(runner, "scratched", False)) if runner else False
        title = f"{no or '—'} {rr.name}{mark} · {rr.score:.3f}"
        if scratched:
            title += " · SCRATCHED"
        with st.expander(title, expanded=(rr.rank <= 2)):
            if runner is None:
                st.caption("No runner record.")
                continue
            o = odds_lookup(view.venue_raw or view.venue, view.race_no, rr.name) if odds_lookup else None
            flucs = ""
            win_odds = ""
            if o:
                if o.get("win") is not None:
                    win_odds = f"${float(o['win']):.1f}"
                flucs = str(o.get("fluc") or "")
                extra = o.get("flucs") or []
                if extra:
                    flucs = f"{flucs} {extra}".strip()
            last_cls = runner_last_class(runner)
            arrow = runner_class_arrow(runner, view.race_class)
            st.write(
                f"**No/barrier:** {no or '—'} / {runner.draw if runner.draw is not None else '—'}  \n"
                f"**Odds:** {win_odds or '—'} {flucs}  \n"
                f"**Form:** {runner.last10 or '—'}  \n"
                f"**Class:** {last_cls or '—'} {arrow}  \n"
                f"**Weight:** {runner.weight_kg if runner.weight_kg is not None else '—'}  \n"
                f"**Jockey:** {runner.jockey_or_driver or '—'}  \n"
                f"**Trainer:** {runner.trainer or '—'}  \n"
                f"**Score:** {rr.score:.3f} · {rr.key_factors}"
            )
            if rr.why_bullets:
                st.write("**Why**")
                for b in rr.why_bullets:
                    st.write(f"- {b}")
            with st.expander("Score components", expanded=False):
                st.json(rr.debug)

    if not ranked:
        st.warning("No active runners to rank for this race.")
    with st.expander("Auto-weight rationale", expanded=False):
        for line in rationale:
            st.write(f"- {line}")
