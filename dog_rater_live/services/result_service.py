"""Match saved pick snapshots to persisted race results.

Name matching is exact after normalisation. Loose / substring matches are never
used to assign a result. Uncertain matches are logged and surfaced as
RESULT UNAVAILABLE rather than a silent win/loss.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from services.formatting import ordinal
from services.names import names_are_matchable, names_match, normalize_runner_name

log = logging.getLogger("dog_rater_live.results")

PENDING = "PENDING"
AWAITING_RESULT = "AWAITING RESULT"
WIN = "WIN"
PLACED = "PLACED"
LOST = "LOST"
PRIMARY_SCRATCHED = "PRIMARY SCRATCHED"
BACKUP_WON = "BACKUP WON"
VOID = "VOID"
RESULT_UNAVAILABLE = "RESULT UNAVAILABLE"

TERMINAL_FOR_STRIKE = frozenset({WIN, PLACED, LOST})
COMPLETED_STATUSES = frozenset({WIN, PLACED, LOST, BACKUP_WON, PRIMARY_SCRATCHED, VOID, RESULT_UNAVAILABLE})


@dataclass(frozen=True)
class ResolvedPick:
    status: str
    primary_finish: Optional[int]
    backup_finish: Optional[int]
    primary_finish_label: str
    backup_finish_label: str
    result_source: str
    match_note: str
    fetch_failed: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "primary_finish": self.primary_finish,
            "backup_finish": self.backup_finish,
            "primary_finish_label": self.primary_finish_label,
            "backup_finish_label": self.backup_finish_label,
            "result_source": self.result_source,
            "match_note": self.match_note,
            "fetch_failed": self.fetch_failed,
        }


def _finish_for(name: str, winner: str, place2: str, place3: str) -> tuple[Optional[int], str]:
    if not names_are_matchable(name):
        return None, "uncertain_name"
    if winner and names_match(name, winner):
        return 1, "matched"
    if place2 and names_match(name, place2):
        return 2, "matched"
    if place3 and names_match(name, place3):
        return 3, "matched"
    return None, "unmatched"


def _top3_complete(winner: str, place2: str, place3: str) -> bool:
    return bool(normalize_runner_name(winner) and normalize_runner_name(place2) and normalize_runner_name(place3))


def race_has_jumped(now: Optional[datetime], jump_at: Optional[datetime]) -> bool:
    if now is None or jump_at is None:
        return False
    return now >= jump_at


def resolve_pick_result(
    pick: dict[str, Any],
    result: Optional[dict[str, Any]],
    *,
    now: Optional[datetime] = None,
    jump_at: Optional[datetime] = None,
    fetch_failed: bool = False,
    jumped: Optional[bool] = None,
) -> ResolvedPick:
    """Assign an explicit dashboard status from a saved pick + optional result row."""
    result = result or {}
    winner = str(result.get("winner") or "")
    place2 = str(result.get("place2") or "")
    place3 = str(result.get("place3") or "")
    result_status = str(result.get("status") or "").lower()
    error_message = str(result.get("error_message") or "")
    source = str(result.get("source_url") or "")

    if jumped is None:
        jumped = race_has_jumped(now, jump_at)

    if result_status == "void":
        return ResolvedPick(
            VOID, None, None, "—", "—", source or "void", "void", fetch_failed=False
        )

    failed = fetch_failed or result_status == "error"
    if failed and not winner:
        log.info(
            "Result unavailable for %s R%s: %s",
            pick.get("venue"),
            pick.get("race_no"),
            error_message or "fetch failed",
        )
        return ResolvedPick(
            RESULT_UNAVAILABLE,
            None,
            None,
            "—",
            "—",
            source,
            error_message or "fetch_failed",
            fetch_failed=True,
        )

    if not jumped:
        return ResolvedPick(PENDING, None, None, "—", "—", "", "pending")

    primary = str(pick.get("pick_name") or pick.get("original_primary") or "")
    if pick.get("original_primary"):
        primary = str(pick.get("original_primary") or primary)
    backup = str(pick.get("backup") or "")
    primary_scratched = bool(pick.get("primary_scratched"))

    if not winner:
        return ResolvedPick(AWAITING_RESULT, None, None, "—", "—", source, "awaiting")

    p_fin, p_note = _finish_for(primary, winner, place2, place3)
    b_fin, b_note = _finish_for(backup, winner, place2, place3) if backup else (None, "no_backup")

    if p_note == "uncertain_name":
        log.warning(
            "Uncertain primary name for %s R%s: %r",
            pick.get("venue"),
            pick.get("race_no"),
            primary,
        )
        return ResolvedPick(
            RESULT_UNAVAILABLE,
            None,
            b_fin,
            "—",
            ordinal(b_fin),
            source,
            "uncertain_primary_name",
        )

    if primary_scratched:
        if b_fin == 1:
            return ResolvedPick(
                BACKUP_WON,
                None,
                1,
                "SCR",
                "1st",
                source,
                "backup_promoted_won",
            )
        return ResolvedPick(
            PRIMARY_SCRATCHED,
            None,
            b_fin,
            "SCR",
            ordinal(b_fin),
            source,
            "primary_scratched",
        )

    if p_fin == 1:
        return ResolvedPick(WIN, 1, b_fin, "1st", ordinal(b_fin), source, "primary_win")
    if p_fin in (2, 3):
        return ResolvedPick(PLACED, p_fin, b_fin, ordinal(p_fin), ordinal(b_fin), source, "primary_placed")

    if p_note == "unmatched":
        if _top3_complete(winner, place2, place3):
            log.info(
                "Primary unmatched in top 3 for %s R%s pick=%r winner=%r",
                pick.get("venue"),
                pick.get("race_no"),
                primary,
                winner,
            )
            return ResolvedPick(LOST, None, b_fin, "unplaced", ordinal(b_fin), source, "unplaced")
        log.warning(
            "Could not match primary %r to incomplete result %r/%r/%r (%s R%s)",
            primary,
            winner,
            place2,
            place3,
            pick.get("venue"),
            pick.get("race_no"),
        )
        return ResolvedPick(
            RESULT_UNAVAILABLE,
            None,
            b_fin,
            "—",
            ordinal(b_fin),
            source,
            "unmatched_incomplete_result",
        )

    return ResolvedPick(LOST, None, b_fin, "unplaced", ordinal(b_fin), source, "lost")


@dataclass(frozen=True)
class DailySummary:
    completed: int
    primary_wins: int
    primary_places: int
    backup_wins: int
    win_strike_rate: Optional[float]
    place_strike_rate: Optional[float]
    estimated_win_return: Optional[float]
    estimated_return_label: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "completed": self.completed,
            "primary_wins": self.primary_wins,
            "primary_places": self.primary_places,
            "backup_wins": self.backup_wins,
            "win_strike_rate": self.win_strike_rate,
            "place_strike_rate": self.place_strike_rate,
            "estimated_win_return": self.estimated_win_return,
            "estimated_return_label": self.estimated_return_label,
        }


def daily_summary(resolved_rows: list[dict[str, Any]]) -> DailySummary:
    """Strike rates from saved picks + confirmed results only.

    Primary scratchings and unavailable results are excluded from the
    denominator. Estimated $1 win-only return is omitted unless every
    eligible row has a saved decimal win price.
    """
    eligible = [r for r in resolved_rows if r.get("status") in TERMINAL_FOR_STRIKE]
    backup_wins = sum(1 for r in resolved_rows if r.get("status") == BACKUP_WON)
    completed = len(eligible)
    wins = sum(1 for r in eligible if r.get("status") == WIN)
    places = sum(1 for r in eligible if r.get("status") in (WIN, PLACED))
    win_rate = (wins / completed) if completed else None
    place_rate = (places / completed) if completed else None

    odds_ok = True
    unit_return = 0.0
    if not eligible:
        odds_ok = False
    for r in eligible:
        odds = r.get("primary_odds")
        try:
            price = float(odds)
        except (TypeError, ValueError):
            odds_ok = False
            break
        if price <= 1.0:
            odds_ok = False
            break
        if r.get("status") == WIN:
            unit_return += price - 1.0
        else:
            unit_return -= 1.0

    return DailySummary(
        completed=completed,
        primary_wins=wins,
        primary_places=places,
        backup_wins=backup_wins,
        win_strike_rate=win_rate,
        place_strike_rate=place_rate,
        estimated_win_return=round(unit_return, 2) if odds_ok else None,
        estimated_return_label="Estimated $1 win-only return" if odds_ok else "",
    )


def by_confidence(resolved_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[str, dict[str, int]] = {}
    for r in resolved_rows:
        label = str(r.get("confidence_label") or "—")
        b = buckets.setdefault(label, {"label": label, "n": 0, "wins": 0, "places": 0})
        if r.get("status") not in TERMINAL_FOR_STRIKE:
            continue
        b["n"] += 1
        if r.get("status") == WIN:
            b["wins"] += 1
        if r.get("status") in (WIN, PLACED):
            b["places"] += 1
    out = []
    for b in buckets.values():
        n = b["n"]
        out.append(
            {
                **b,
                "win_rate": (b["wins"] / n) if n else None,
                "place_rate": (b["places"] / n) if n else None,
            }
        )
    return sorted(out, key=lambda x: x["label"])


def sync_missing_results(
    *,
    chosen_date,
    views,
    now,
    fetch_meeting_results,
    load_results_fn,
    persist_results_fn,
    persist_failure_fn,
    db_path=None,
) -> dict:
    """Fetch and persist results for jumped races that don't yet have a winner.

    Distinguishes empty published results (awaiting) from fetch exceptions.
    """
    jumped_by_meeting: dict[tuple[str, str], list[int]] = {}
    for v in views:
        if v.jump_at is None or v.jump_at > now:
            continue
        jumped_by_meeting.setdefault((v.meeting_url, v.code), []).append(v.race_no)

    fetched = 0
    errors = 0
    for (meeting_url, code), race_nos in jumped_by_meeting.items():
        stored = load_results_fn(chosen_date, meeting_url, code, db_path=db_path) if db_path is not None else load_results_fn(chosen_date, meeting_url, code)
        missing = [rn for rn in race_nos if not (stored.get(rn) or stored.get(int(rn)) or {}).get("winner")]
        if not missing:
            continue
        try:
            results = fetch_meeting_results(code, meeting_url) or {}
            persist_kw = {"db_path": db_path} if db_path is not None else {}
            persist_results_fn(chosen_date, meeting_url, code, results, **persist_kw)
            fetched += 1
        except Exception as e:
            errors += 1
            persist_kw = {"db_path": db_path} if db_path is not None else {}
            for rn in missing:
                persist_failure_fn(chosen_date, meeting_url, code, rn, str(e), **persist_kw)
            log.warning("Result fetch failed for %s: %s", meeting_url, e)
    return {"meetings_fetched": fetched, "errors": errors}
