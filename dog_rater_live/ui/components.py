"""Shared Streamlit presentation helpers."""

from __future__ import annotations

import html as html_lib
from typing import Optional

import streamlit as st

from services.formatting import format_runner_pick
from services.result_service import (
    AWAITING_RESULT,
    BACKUP_PROMOTED,
    BACKUP_WON,
    BOTH_SCRATCHED,
    LOST,
    NO_ACTIVE_SELECTION,
    PENDING,
    PLACED,
    PRIMARY_SCRATCHED,
    RESULT_UNAVAILABLE,
    VOID,
    WIN,
)

_STATUS_STYLE = {
    WIN: ("#1b5e20", "#e8f5e9"),
    PLACED: ("#2e7d32", "#e8f5e9"),
    BACKUP_WON: ("#4a148c", "#f3e5f5"),
    LOST: ("#b71c1c", "#fbe9e7"),
    PENDING: ("#e65100", "#fff3e0"),
    AWAITING_RESULT: ("#ef6c00", "#fff8e1"),
    PRIMARY_SCRATCHED: ("#616161", "#eeeeee"),
    BACKUP_PROMOTED: ("#4a148c", "#f3e5f5"),
    BOTH_SCRATCHED: ("#b71c1c", "#fbe9e7"),
    NO_ACTIVE_SELECTION: ("#b71c1c", "#fbe9e7"),
    VOID: ("#616161", "#eeeeee"),
    RESULT_UNAVAILABLE: ("#546e7a", "#eceff1"),
}

_URGENCY = {
    "amber": "#ef6c00",
    "red": "#c62828",
    "grey": "#9e9e9e",
    "green": "#2e7d32",
}


def inject_base_css() -> None:
    st.markdown(
        """
<style>
  .rd-hero {border:1px solid #3d3d3d;border-radius:12px;padding:1rem 1.2rem;background:#161616;}
  .rd-kicker {color:#9aa0a6;font-size:0.8rem;letter-spacing:0.04em;text-transform:uppercase;}
  .rd-muted {color:#9aa0a6;font-size:0.85rem;}
  .rd-metric {padding:0.4rem 0;}
  .rd-pick {font-size:1.2rem;font-weight:650;line-height:1.35;margin-top:0.15rem;white-space:normal;}
</style>
""",
        unsafe_allow_html=True,
    )


def status_badge(status: str) -> str:
    fg, bg = _STATUS_STYLE.get(status, ("#333", "#eee"))
    label = status or "—"
    return (
        f'<span style="display:inline-block;padding:2px 8px;border-radius:999px;'
        f'font-size:0.75rem;font-weight:650;color:{fg};background:{bg};">{label}</span>'
    )


def md_badge(status: str) -> None:
    st.markdown(status_badge(status), unsafe_allow_html=True)


def data_status_chip(status: str, last_ok: Optional[str]) -> None:
    colour = {"ok": "#2e7d32", "loading": "#ef6c00", "error": "#c62828"}.get(status, "#9e9e9e")
    label = {"ok": "Data OK", "loading": "Loading", "error": "Data error", "idle": "Idle"}.get(status, status)
    extra = f" · last update {last_ok}" if last_ok else ""
    st.markdown(
        f'<span style="color:{colour};font-weight:650;">● {label}</span>'
        f'<span class="rd-muted">{extra}</span>',
        unsafe_allow_html=True,
    )


def pick_cell(no, name: str, odds: Optional[float] = None) -> str:
    return format_runner_pick(number=no, name=name, odds=odds)


def pick_metric_html(label: str, value: str) -> str:
    """HTML for a labelled pick. Avoids st.metric, which turns '5. NAME' into a markdown list."""
    return (
        f'<div class="rd-metric"><div class="rd-kicker">{html_lib.escape(label)}</div>'
        f'<div class="rd-pick">{html_lib.escape(value)}</div></div>'
    )


def pick_metric(label: str, value: str) -> None:
    st.markdown(pick_metric_html(label, value), unsafe_allow_html=True)


def urgency_style(token: str) -> str:
    return _URGENCY.get(token, "")
