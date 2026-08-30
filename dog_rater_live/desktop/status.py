"""Load stages and safe UI status copy (no tracebacks)."""

from __future__ import annotations

from pathlib import Path

STARTING = "STARTING"
LOADING_CACHE = "LOADING_CACHE"
LOADING_CARD = "LOADING_CARD"
CARD_READY = "CARD_READY"
ENRICHING_ODDS = "ENRICHING_ODDS"
CHECKING_RESULTS = "CHECKING_RESULTS"
PARTIAL = "PARTIAL"
OFFLINE_CACHED = "OFFLINE_CACHED"
EMPTY = "EMPTY"
ERROR = "ERROR"

LOADING_STAGES = {STARTING, LOADING_CACHE, LOADING_CARD}
LOADING_MESSAGE = "Loading today's thoroughbred meetings…"
EMPTY_MESSAGE = (
    "No thoroughbred card loaded. If you are offline and have never loaded this date, "
    "connect to the internet and press Refresh."
)

STAGE_STATUS = {
    STARTING: LOADING_MESSAGE,
    LOADING_CACHE: LOADING_MESSAGE,
    LOADING_CARD: "Loading meetings",
    CARD_READY: "Card ready",
    ENRICHING_ODDS: "Updating odds",
    CHECKING_RESULTS: "Checking results",
    PARTIAL: "Partial source failure",
    OFFLINE_CACHED: "Cached — refreshing",
    EMPTY: "No meetings found for selected date",
    ERROR: "Refresh failed",
}


def is_loading(stage: str) -> bool:
    return stage in LOADING_STAGES


def safe_error_summary(exc: BaseException, *, kind: str = "", db_path: Path | None = None) -> str:
    text = str(exc or "").strip().splitlines()[0] if exc else ""
    lower = text.lower()
    if db_path is not None and (
        "database" in lower or "sqlite" in lower or "unable to open" in lower
    ):
        return f"Database could not be opened: {db_path}"
    if kind == "odds" or "odds" in lower:
        return "Fields loaded; odds currently unavailable"
    if kind == "results" or "result" in lower:
        return "Result check failed; persisted results kept"
    if "timeout" in lower or "timed out" in lower:
        if "qld" in lower:
            return "Racing Australia calendar unavailable for QLD"
        return "Racing Australia calendar unavailable"
    if "network" in lower or "connection" in lower or "failed to establish" in lower:
        return "Network unavailable and no cached card exists" if kind in {"card", "cached", ""} else text[:180]
    if text:
        return text[:180]
    return "Refresh failed"


def empty_label(stage: str, has_views: bool, error_summary: str = "") -> str:
    if has_views:
        return ""
    if is_loading(stage) or stage in {ENRICHING_ODDS, CHECKING_RESULTS}:
        return LOADING_MESSAGE
    if stage == ERROR and error_summary:
        return error_summary
    if stage in {EMPTY, ERROR}:
        return EMPTY_MESSAGE if stage == EMPTY else (error_summary or EMPTY_MESSAGE)
    return LOADING_MESSAGE
