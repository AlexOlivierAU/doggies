"""Model lab: heuristic weights, rationale, compression backtest."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import streamlit as st

from backtest_compression import format_report, run_backtest
from services.race_day_service import build_race_views
from services.ranking import rank_field
from ui.components import inject_base_css


def render_model(
    *,
    chosen_date: date,
    meetings: list,
    fields_by_meeting: dict,
    now: datetime,
    app_tz: ZoneInfo,
) -> None:
    inject_base_css()
    st.title("Model")
    st.info(
        "Scoring is **adaptive heuristic weighting** (form, draw, class/weight, conditions). "
        "It is not a trained machine-learning model and it is not betting advice."
    )

    st.subheader("Weight controls")
    auto = st.toggle("Auto weights (recommended)", value=True, key="model_auto_weights")
    c1, c2, c3 = st.columns(3)
    with c1:
        box_w = st.slider("Draw weight", 0.0, 1.0, 0.20, 0.01, disabled=auto, key="model_box_w")
    with c2:
        form_w = st.slider("Form weight", 0.0, 1.0, 0.50, 0.01, disabled=auto, key="model_form_w")
    with c3:
        early_w = st.slider("Class/weight proxy", 0.0, 1.0, 0.30, 0.01, disabled=auto, key="model_early_w")

    views = build_race_views(
        chosen_date=chosen_date,
        meetings=meetings,
        fields_by_meeting=fields_by_meeting,
        now=now,
        app_tz=app_tz,
        state_filter=str(st.session_state.get("state_filter") or "All"),
        rank_upcoming_only=False,
    )
    if views:
        labels = [f"{v.venue} R{v.race_no} · {v.clock()}" for v in views]
        idx = st.selectbox("Inspect race", options=list(range(len(views))), format_func=lambda i: labels[i], key="model_race")
        view = views[int(idx)]
        kwargs: dict[str, Any] = {"track_condition": view.meta.get("track_condition")}
        if not auto:
            kwargs.update(box_weight=box_w, form_weight=form_w, early_weight=early_w)
        ranked, weights, rationale = rank_field(view.runners, **kwargs)
        st.caption(f"Weights in use: draw={weights[0]:.2f}, form={weights[1]:.2f}, proxy={weights[2]:.2f}")
        with st.expander("Auto-weight rationale", expanded=True):
            for line in rationale:
                st.write(f"- {line}")
        st.write("**Ranked runners**")
        st.dataframe(
            [
                {
                    "rank": r.rank,
                    "runner": r.name,
                    "barrier": r.draw,
                    "score": round(r.score, 3),
                    "key factors": r.key_factors,
                }
                for r in ranked
            ],
            width="stretch",
            hide_index=True,
        )
        with st.expander("Detailed score components"):
            for r in ranked[:8]:
                st.write(f"**{r.rank}. {r.name}**")
                st.json(r.debug)
    else:
        st.caption("Load Race Day so a field is available to inspect.")

    st.subheader("Compression backtest (thoroughbred)")
    st.caption(
        "Measures whether small Rank1−Rank2/3 score gaps line up with place-heavy outcomes. "
        "This uses live public result pages and can be slow. It is an exploratory diagnostic."
    )
    days_back = st.slider("Days to look back", min_value=1, max_value=28, value=7, key="bt_days")
    percentile = st.slider("Clustered threshold (percentile)", min_value=10, max_value=50, value=25, key="bt_pct")
    if st.button("Run backtest"):
        with st.spinner("Fetching meetings, fields, and results..."):
            end_d = chosen_date
            start_d = end_d - __import__("datetime").timedelta(days=days_back)
            metrics, threshold, summary = run_backtest(
                start_d, end_d, threshold_percentile=float(percentile), ttl_seconds=120
            )
        st.code(format_report(summary), language=None)
        if metrics:
            st.caption(f"{len(metrics)} races in this range.")
        else:
            st.info("No thoroughbred races with results in this range.")
