"""Historical review of saved pick snapshots and confirmed results."""

from __future__ import annotations

import html as html_lib
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import streamlit as st

from race_db import get_pick, load_picks_range, load_results_range
from services.confidence import LABEL_CLOSE, LABEL_MEDIUM, LABEL_STRONG
from services.formatting import format_saved_selection
from services.result_service import (
    AWAITING_RESULT,
    BACKUP_WON,
    LOST,
    PENDING,
    PLACED,
    PRIMARY_SCRATCHED,
    RESULT_UNAVAILABLE,
    VOID,
    WIN,
    by_confidence,
    daily_summary,
    resolve_pick_result,
)
from ui.components import inject_base_css, status_badge


_STATUSES = [
    "All",
    PENDING,
    AWAITING_RESULT,
    WIN,
    PLACED,
    LOST,
    PRIMARY_SCRATCHED,
    BACKUP_WON,
    VOID,
    RESULT_UNAVAILABLE,
]


def render_history(*, app_tz: ZoneInfo, default_date: date) -> None:
    inject_base_css()
    st.title("History")
    st.caption("Saved snapshots only. Old races are not re-scored with the current model.")

    today = default_date
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        date_from = st.date_input("From", value=today - timedelta(days=7), key="hist_from")
    with c2:
        date_to = st.date_input("To", value=today, key="hist_to")
    with c3:
        status_f = st.selectbox("Result status", _STATUSES, key="hist_status")
    with c4:
        conf_f = st.selectbox("Confidence", ["All", LABEL_STRONG, LABEL_MEDIUM, LABEL_CLOSE], key="hist_conf")

    if date_from > date_to:
        st.error("From date must be on or before To date.")
        return

    picks = load_picks_range(date_from, date_to)
    results = load_results_range(date_from, date_to)
    venues = sorted({str(p.get("venue") or "") for p in picks if p.get("venue")})
    venue_f = st.selectbox("Venue", ["All", *venues], key="hist_venue")

    now = datetime.now(app_tz)
    rows = []
    for p in picks:
        if (p.get("code") or "thoroughbred") != "thoroughbred":
            continue
        if venue_f != "All" and str(p.get("venue") or "") != venue_f:
            continue
        try:
            d = date.fromisoformat(str(p.get("meeting_date") or p.get("date") or today.isoformat()))
        except Exception:
            d = today
        key = (str(p.get("meeting_date") or d.isoformat()), str(p.get("meeting_url") or ""), int(p.get("race_no") or 0))
        result = results.get(key) or {}
        jump = None
        sj = p.get("scheduled_jump") or ""
        if sj:
            try:
                jump = datetime.fromisoformat(sj)
            except Exception:
                jump = None
        jumped = None
        if jump is None:
            jumped = bool(result.get("winner")) or d < date.today()
        resolved = resolve_pick_result(p, result, now=now, jump_at=jump, jumped=jumped)
        rec = {
            **p,
            **resolved.as_dict(),
            "date": d.isoformat(),
            "confidence_label": p.get("confidence_label") or "",
        }
        if status_f != "All" and rec["status"] != status_f:
            continue
        if conf_f != "All" and rec["confidence_label"] != conf_f:
            continue
        rows.append(rec)

    summary = daily_summary(rows)
    m = st.columns(6)
    m[0].metric("Completed", summary.completed)
    m[1].metric("Primary wins", summary.primary_wins)
    m[2].metric("Primary places", summary.primary_places)
    m[3].metric("Backup wins", summary.backup_wins)
    m[4].metric("Win SR", f"{summary.win_strike_rate:.0%}" if summary.win_strike_rate is not None else "—")
    m[5].metric("Place SR", f"{summary.place_strike_rate:.0%}" if summary.place_strike_rate is not None else "—")
    st.caption("Strike rates need a useful sample of confirmed results. This is not a profitability claim.")

    conf_tbl = by_confidence(rows)
    if conf_tbl:
        st.write("**By confidence label**")
        st.dataframe(
            [
                {
                    "Confidence": r["label"],
                    "Completed": r["n"],
                    "Wins": r["wins"],
                    "Places": r["places"],
                    "Win SR": f"{r['win_rate']:.0%}" if r.get("win_rate") is not None else "—",
                    "Place SR": f"{r['place_rate']:.0%}" if r.get("place_rate") is not None else "—",
                }
                for r in conf_tbl
            ],
            width="stretch",
            hide_index=True,
        )

    if not rows:
        st.info("No snapshots match these filters.")
        return

    html = [
        "<table width='100%'><thead><tr>",
        "<th>Date</th><th>Result</th><th>Race</th><th>Primary</th><th>Pos</th>",
        "<th>Odds</th><th>Backup</th><th>Pos</th><th>Conf</th></tr></thead><tbody>",
    ]
    for rec in rows:
        html.append(
            "<tr>"
            f"<td>{html_lib.escape(str(rec.get('date') or ''))}</td>"
            f"<td>{status_badge(rec['status'])}</td>"
            f"<td>{html_lib.escape(str(rec.get('venue') or ''))} R{rec.get('race_no')}</td>"
            f"<td>{html_lib.escape(format_saved_selection(rec, 'primary'))}</td>"
            f"<td>{html_lib.escape(str(rec.get('primary_finish_label') or ''))}</td>"
            f"<td>{html_lib.escape(str(rec.get('primary_odds') if rec.get('primary_odds') is not None else '—'))}</td>"
            f"<td>{html_lib.escape(format_saved_selection(rec, 'backup'))}</td>"
            f"<td>{rec.get('backup_finish_label')}</td>"
            f"<td>{rec.get('confidence_label') or '—'}</td>"
            "</tr>"
        )
    html.append("</tbody></table>")
    st.markdown("".join(html), unsafe_allow_html=True)

    options = [f"{r.get('date')} {r.get('venue')} R{r.get('race_no')}" for r in rows]
    pick_label = st.selectbox("Open snapshot", options, key="hist_open")
    rec = rows[options.index(pick_label)] if pick_label in options else None
    if rec:
        d = date.fromisoformat(str(rec.get("date")))
        snap = get_pick(d, str(rec.get("meeting_url") or ""), int(rec.get("race_no") or 0))
        st.subheader("Saved snapshot")
        if not snap:
            st.json({k: rec.get(k) for k in ("pick_name", "backup", "confidence_label", "status")})
            return
        st.write(
            f"**Locked:** {bool(snap.get('locked'))}  \n"
            f"**Primary:** {format_saved_selection(snap, 'primary')}  \n"
            f"**Backup:** {format_saved_selection(snap, 'backup')}  \n"
            f"**Confidence:** {snap.get('confidence_label')} (gap {snap.get('score_gap')})  \n"
            f"**Odds at selection:** {snap.get('primary_odds')} / backup {snap.get('backup_odds')}  \n"
            f"**Picked at:** {snap.get('picked_at_iso') or snap.get('saved_at')}  \n"
            f"**Primary scratched:** {bool(snap.get('primary_scratched'))} · "
            f"**Backup promoted:** {bool(snap.get('backup_promoted'))}"
        )
        st.write(f"**Confirmed result:** {rec.get('status')} · primary {rec.get('primary_finish_label')} · backup {rec.get('backup_finish_label')}")
        if snap.get("why_bullets"):
            st.write("**Why (saved)**")
            for b in snap.get("why_bullets") or []:
                st.write(f"- {b}")
        if snap.get("weights") or (snap.get("snapshot") or {}).get("weights"):
            st.write("**Weights snapshot**")
            st.json(snap.get("weights") or (snap.get("snapshot") or {}).get("weights"))
        field = (snap.get("snapshot") or {}).get("field") or []
        if field:
            with st.expander("Saved field snapshot"):
                st.dataframe(field, width="stretch", hide_index=True)
