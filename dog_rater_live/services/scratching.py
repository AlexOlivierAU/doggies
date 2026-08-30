"""Effective thoroughbred scratching state.

A runner known to be scratched by any authoritative current source must not be
an active live primary or backup. Missing odds alone is never treated as a scratch.

Sources (union):
  - Racing Australia ``Runner.scratched``
  - Sportsbet ``isOut`` / ``scratched``
  - Persisted confirmed late-scratching state (does not auto-revert if the feed omits the row)

Name matching uses canonical ``names_match`` (no substring / fuzzy matching).
Parser-owned ``Runner`` objects are not mutated; callers receive ``dataclasses.replace`` copies.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Iterable, Optional

from models import Runner
from services.names import names_are_matchable, names_match, normalize_runner_name
from services.ranking import rank_field, selections_from_ranked
from services.runner_numbers import coerce_program_number, program_number_for_runner

log = logging.getLogger("dog_rater_live.scratching")

SOURCE_RA = "racingaustralia"
SOURCE_SPORTSBET = "sportsbet"
SOURCE_PERSISTED = "persisted"

# Process-lifetime idempotency for transition logs (notifications use a separate seen-set).
_LOGGED_TRANSITIONS: set[tuple] = set()


@dataclass
class ScratchRecord:
    name: str
    program_number: Optional[int] = None
    scratched: bool = False
    sources: tuple[str, ...] = ()
    confirmed_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "program_number": self.program_number,
            "scratched": self.scratched,
            "sources": list(self.sources),
            "confirmed_at": self.confirmed_at,
        }


@dataclass
class EffectiveField:
    runners: list[Runner]
    records: dict[str, ScratchRecord]
    unmatched: list[dict[str, Any]] = field(default_factory=list)
    field_size: int = 0

    def is_scratched(self, name: str) -> bool:
        if not name:
            return False
        rec = self.records.get(name)
        if rec and rec.scratched:
            return True
        for rec in self.records.values():
            if rec.scratched and names_match(rec.name, name):
                return True
        return False

    def sources_for(self, name: str) -> tuple[str, ...]:
        rec = self.records.get(name)
        if rec:
            return rec.sources
        for rec in self.records.values():
            if names_match(rec.name, name):
                return rec.sources
        return ()

    def confirmed_at(self, name: str) -> str:
        rec = self.records.get(name)
        if rec:
            return rec.confirmed_at
        for rec in self.records.values():
            if names_match(rec.name, name):
                return rec.confirmed_at
        return ""


@dataclass
class SelectionResolution:
    """Active vs original pick names. ``primary`` on RaceView is the active primary."""

    active_primary: str = ""
    active_primary_no: str = ""
    active_backup: str = ""
    active_backup_no: str = ""
    original_primary: str = ""
    original_primary_no: str = ""
    original_backup: str = ""
    original_backup_no: str = ""
    primary_scratched: bool = False
    backup_scratched: bool = False
    backup_promoted: bool = False
    no_active_selection: bool = False
    selection_warning: str = ""
    from_snapshot: bool = False
    ranked: list = field(default_factory=list)
    weights: tuple[float, float, float] = (1 / 3, 1 / 3, 1 / 3)
    why: list[str] = field(default_factory=list)
    primary_score: Optional[float] = None
    backup_score: Optional[float] = None
    score_gap: float = 0.0
    confidence_label: str = ""


def _iso(now: Optional[datetime]) -> str:
    if now is None:
        return ""
    try:
        return now.isoformat(timespec="seconds")
    except Exception:
        return str(now)


def _as_int_no(value) -> Optional[int]:
    return coerce_program_number(value)


def _numbers_compatible(runner_no, odds_no) -> bool:
    a, b = _as_int_no(runner_no), _as_int_no(odds_no)
    if a is None or b is None:
        return True
    return a == b


def _row_source(row: dict[str, Any]) -> str:
    raw = str(row.get("source") or "").strip().lower()
    if raw in {SOURCE_SPORTSBET, "sb", "isout"}:
        return SOURCE_SPORTSBET
    if raw:
        return raw
    return SOURCE_SPORTSBET


def _explicit_scratch_flag(row: dict[str, Any]) -> bool:
    if not isinstance(row, dict):
        return False
    if row.get("scratched") is True or row.get("isOut") is True:
        return True
    if str(row.get("scratched") or "").lower() in {"1", "true", "yes"}:
        return True
    return False


def _match_odds_row_to_runners(row: dict[str, Any], runners: list[Runner]) -> tuple[Optional[Runner], str]:
    """Return (runner, reason). reason is 'ok', 'unmatched', 'ambiguous', or 'number_mismatch'."""
    name = str(row.get("name") or row.get("horse") or "").strip()
    if not names_are_matchable(name):
        return None, "unmatched"
    hits = [r for r in runners if names_match(getattr(r, "name", ""), name)]
    if not hits:
        return None, "unmatched"
    if len(hits) > 1:
        numbered = [r for r in hits if _numbers_compatible(program_number_for_runner(r), row.get("no") or row.get("program_number"))]
        if len(numbered) == 1:
            return numbered[0], "ok"
        return None, "ambiguous"
    runner = hits[0]
    if not _numbers_compatible(program_number_for_runner(runner), row.get("no") or row.get("program_number")):
        return None, "number_mismatch"
    return runner, "ok"


def persisted_scratch_records(saved: Optional[dict[str, Any]]) -> list[dict[str, Any]]:
    if not saved:
        return []
    out: list[dict[str, Any]] = []
    snap = saved.get("snapshot") if isinstance(saved.get("snapshot"), dict) else {}
    extra = saved.get("pick_data") if isinstance(saved.get("pick_data"), dict) else {}
    inner = extra.get("snapshot") if isinstance(extra.get("snapshot"), dict) else {}
    for blob in (saved.get("scratching_state"), snap.get("scratching_state"), inner.get("scratching_state")):
        for item in blob or []:
            if isinstance(item, dict) and item.get("scratched"):
                out.append(item)
    orig_p = str(saved.get("original_primary") or saved.get("pick_name") or saved.get("best_pick") or "")
    if saved.get("primary_scratched") and orig_p:
        out.append(
            {
                "name": orig_p,
                "program_number": _as_int_no(saved.get("primary_number") or saved.get("original_primary_number")),
                "scratched": True,
                "sources": [SOURCE_PERSISTED],
                "confirmed_at": str(saved.get("scratching_detected_at") or ""),
            }
        )
    if saved.get("backup_scratched"):
        bname = str(saved.get("original_backup") or saved.get("backup") or "")
        if bname:
            out.append(
                {
                    "name": bname,
                    "program_number": _as_int_no(saved.get("backup_number")),
                    "scratched": True,
                    "sources": [SOURCE_PERSISTED],
                    "confirmed_at": str(saved.get("scratching_detected_at") or ""),
                }
            )
    return out


def effective_scratching_state(
    runners: Iterable[Runner] | None,
    odds_rows: Iterable[dict[str, Any]] | None = None,
    persisted_scratches: Iterable[dict[str, Any]] | None = None,
    *,
    venue: str = "",
    race_no: Any = None,
    now: Optional[datetime] = None,
) -> EffectiveField:
    """Union RA / Sportsbet / persisted explicit scratch flags. Does not mutate inputs."""
    base = list(runners or [])
    confirmed = _iso(now)
    by_name: dict[str, ScratchRecord] = {}

    def mark(runner: Runner, source: str, *, at: str = "", number=None) -> None:
        name = str(getattr(runner, "name", "") or "")
        rec = by_name.get(name) or ScratchRecord(
            name=name,
            program_number=program_number_for_runner(runner),
        )
        sources = list(rec.sources)
        if source and source not in sources:
            sources.append(source)
        rec.scratched = True
        rec.sources = tuple(sources)
        if number is not None and rec.program_number is None:
            rec.program_number = _as_int_no(number)
        if at and not rec.confirmed_at:
            rec.confirmed_at = at
        elif not rec.confirmed_at:
            rec.confirmed_at = confirmed
        by_name[name] = rec

    for runner in base:
        if bool(getattr(runner, "scratched", False)):
            mark(runner, SOURCE_RA, at=confirmed)

    unmatched: list[dict[str, Any]] = []
    for row in odds_rows or []:
        if not _explicit_scratch_flag(row):
            continue
        runner, reason = _match_odds_row_to_runners(row, base)
        if runner is None:
            payload = {
                "venue": venue,
                "race_no": race_no,
                "horse": str(row.get("name") or row.get("horse") or ""),
                "program_number": row.get("no") or row.get("program_number"),
                "normalised": normalize_runner_name(str(row.get("name") or row.get("horse") or "")),
                "reason": reason,
                "source": _row_source(row),
            }
            unmatched.append(payload)
            log.info(
                "Unmatched Sportsbet scratching: venue=%s race=%s horse=%r no=%s normalised=%r reason=%s",
                venue,
                race_no,
                payload["horse"],
                payload["program_number"],
                payload["normalised"],
                reason,
            )
            continue
        mark(runner, _row_source(row), at=confirmed, number=row.get("no") or row.get("program_number"))

    for item in persisted_scratches or []:
        if not isinstance(item, dict) or not item.get("scratched"):
            continue
        fake = {
            "name": item.get("name") or item.get("horse"),
            "no": item.get("program_number") or item.get("no"),
            "scratched": True,
            "source": SOURCE_PERSISTED,
        }
        runner, reason = _match_odds_row_to_runners(fake, base)
        if runner is None:
            continue
        sources = item.get("sources") or [SOURCE_PERSISTED]
        if isinstance(sources, str):
            sources = [sources]
        for src in sources or [SOURCE_PERSISTED]:
            mark(runner, str(src), at=str(item.get("confirmed_at") or confirmed), number=fake.get("no"))

    copies: list[Runner] = []
    for runner in base:
        name = str(getattr(runner, "name", "") or "")
        rec = by_name.get(name)
        scratched = bool(rec.scratched) if rec else bool(getattr(runner, "scratched", False))
        extra = dict(getattr(runner, "raw", None) or {})
        if rec:
            extra["_scratch"] = rec.as_dict()
        copies.append(replace(runner, scratched=scratched, raw=extra))

    field_size = sum(1 for r in copies if not bool(getattr(r, "scratched", False)))
    return EffectiveField(runners=copies, records=by_name, unmatched=unmatched, field_size=field_size)


def _number_for(runners: list[Runner], name: str) -> str:
    if not name:
        return ""
    for r in runners:
        if names_match(getattr(r, "name", ""), name) or getattr(r, "name", "") == name:
            n = program_number_for_runner(r)
            return str(n) if n is not None else ""
    return ""


def _next_active(ranked: list, *, exclude: set[str]) -> str:
    for rr in ranked or []:
        name = str(getattr(rr, "name", "") or "")
        if name and not any(names_match(name, x) for x in exclude if x):
            return name
    return ""


def _sel_scores(ranked: list, name: str) -> Optional[float]:
    if not name:
        return None
    for rr in ranked or []:
        if names_match(getattr(rr, "name", ""), name) or getattr(rr, "name", "") == name:
            try:
                return float(getattr(rr, "score", 0.0) or 0.0)
            except (TypeError, ValueError):
                return None
    return None


def resolve_live_selection(
    *,
    effective: EffectiveField,
    saved: Optional[dict[str, Any]],
    phase: str,
    locked: bool,
    track_condition: Optional[str] = None,
) -> SelectionResolution:
    """Rank active runners, then apply unlocked rerank vs locked promotion rules.

    phase: upcoming | in_progress | finished | unknown
    """
    ranked, weights, _rationale = rank_field(effective.runners, track_condition=track_condition)
    live = selections_from_ranked(ranked)
    out = SelectionResolution(ranked=ranked, weights=weights, why=list(live.get("primary_why") or []))

    use_saved_names = bool(saved) and (locked or phase in {"in_progress", "finished"})
    original_primary = ""
    original_backup = ""
    original_primary_no = ""
    original_backup_no = ""
    if saved:
        original_primary = str(saved.get("original_primary") or saved.get("pick_name") or saved.get("best_pick") or "")
        original_backup = str(saved.get("original_backup") or "")
        if not original_backup:
            # Only treat current backup as original if it was never promoted away.
            if not saved.get("backup_promoted"):
                original_backup = str(saved.get("backup") or "")
        original_primary_no = str(saved.get("primary_number") or saved.get("original_primary_number") or "") or _number_for(
            effective.runners, original_primary
        )
        original_backup_no = str(saved.get("backup_number") or "") or _number_for(effective.runners, original_backup)

    if not use_saved_names:
        # Unlocked live (typically upcoming): full rerank. Do not merely promote backup.
        out.active_primary = str(live.get("primary") or "")
        out.active_backup = str(live.get("backup") or "")
        out.active_primary_no = _number_for(effective.runners, out.active_primary)
        out.active_backup_no = _number_for(effective.runners, out.active_backup)
        out.original_primary = original_primary or out.active_primary
        out.original_backup = original_backup or out.active_backup
        out.original_primary_no = original_primary_no or out.active_primary_no
        out.original_backup_no = original_backup_no or out.active_backup_no
        out.primary_scratched = bool(original_primary) and effective.is_scratched(original_primary)
        out.backup_scratched = bool(original_backup) and effective.is_scratched(original_backup)
        out.backup_promoted = out.primary_scratched and bool(out.active_primary) and not names_match(
            out.active_primary, original_primary
        )
        out.primary_score = live.get("primary_score")
        out.backup_score = live.get("backup_score")
        out.score_gap = float(live.get("score_gap") or 0.0)
        out.confidence_label = str(live.get("confidence_label") or "")
        out.no_active_selection = not bool(out.active_primary)
        if out.no_active_selection:
            out.selection_warning = "NO ACTIVE SELECTION"
        elif out.primary_scratched:
            src = ",".join(effective.sources_for(original_primary)) or "unknown"
            at = effective.confirmed_at(original_primary)
            out.selection_warning = (
                f"Original primary {_number_for(effective.runners, original_primary) or original_primary} "
                f"{original_primary} scratched via {src}"
                + (f" at {at}" if at else "")
                + (f". {out.active_primary} promoted." if out.active_primary else "")
            )
        return out

    # Locked / in-progress / finished: preserve original evidence; promote don't invent a post-result model pick.
    out.from_snapshot = True
    out.original_primary = original_primary
    out.original_backup = original_backup
    out.original_primary_no = original_primary_no or _number_for(effective.runners, original_primary)
    out.original_backup_no = original_backup_no or _number_for(effective.runners, original_backup)
    out.primary_scratched = bool(original_primary) and effective.is_scratched(original_primary)
    out.backup_scratched = bool(original_backup) and effective.is_scratched(original_backup)

    if phase == "finished" and not out.primary_scratched and not out.backup_scratched:
        out.active_primary = original_primary
        out.active_backup = original_backup
        out.active_primary_no = out.original_primary_no
        out.active_backup_no = out.original_backup_no
        out.primary_score = saved.get("pick_score") or saved.get("best_score") if saved else None
        cond = (saved or {}).get("conditions") or {}
        out.backup_score = cond.get("backup_score") if isinstance(cond, dict) else (saved or {}).get("backup_score")
        out.score_gap = float((saved or {}).get("score_gap") or 0.0)
        out.confidence_label = str((saved or {}).get("confidence_label") or "")
        out.no_active_selection = not bool(out.active_primary)
        return out

    exclude: set[str] = set()
    if out.primary_scratched:
        if original_backup and not out.backup_scratched:
            out.active_primary = original_backup
            out.backup_promoted = True
            exclude = {original_primary, original_backup}
            out.active_backup = _next_active(ranked, exclude=exclude)
        else:
            out.backup_promoted = False
            exclude = {original_primary, original_backup}
            out.active_primary = _next_active(ranked, exclude=exclude)
            out.active_backup = _next_active(ranked, exclude=exclude | {out.active_primary})
    else:
        out.active_primary = original_primary
        if out.backup_scratched:
            out.active_backup = _next_active(ranked, exclude={original_primary, original_backup})
        else:
            out.active_backup = original_backup

    out.active_primary_no = (
        out.original_primary_no
        if (
            not out.primary_scratched
            and out.original_primary_no
            and out.active_primary
            and names_match(out.active_primary, original_primary)
        )
        else _number_for(effective.runners, out.active_primary)
    )
    out.active_backup_no = (
        out.original_backup_no
        if (
            not out.backup_scratched
            and out.original_backup_no
            and out.active_backup
            and names_match(out.active_backup, original_backup)
        )
        else _number_for(effective.runners, out.active_backup)
    )
    out.primary_score = _sel_scores(ranked, out.active_primary)
    out.backup_score = _sel_scores(ranked, out.active_backup)
    from services.confidence import confidence_from_scores

    gap, label = confidence_from_scores(out.primary_score, out.backup_score)
    out.score_gap = gap
    out.confidence_label = label if out.active_primary else ""
    out.no_active_selection = not bool(out.active_primary)
    if out.no_active_selection:
        out.selection_warning = "NO ACTIVE SELECTION"
    elif out.primary_scratched:
        src = ",".join(effective.sources_for(original_primary)) or "unknown"
        at = effective.confirmed_at(original_primary)
        out.selection_warning = (
            f"Original primary {out.original_primary_no or ''} {original_primary} scratched via {src}"
            + (f" at {at}" if at else "")
            + (f". {out.active_primary} promoted." if out.active_primary else "")
        ).replace("  ", " ").strip()
    elif out.backup_scratched:
        src = ",".join(effective.sources_for(original_backup)) or "unknown"
        out.selection_warning = f"Original backup {original_backup} scratched via {src}."
    if out.primary_scratched and out.backup_scratched and not out.active_primary:
        out.selection_warning = "NO ACTIVE SELECTION"
    return out


def log_late_scratch_transition(
    *,
    venue: str,
    race_no: Any,
    horse: str,
    source: str,
    locked: bool,
    new_primary: str,
    new_backup: str,
) -> bool:
    """Log one transition per process. Returns True if this call logged."""
    key = (str(venue), int(race_no or 0), str(horse), str(source), str(new_primary), str(locked))
    if key in _LOGGED_TRANSITIONS:
        return False
    _LOGGED_TRANSITIONS.add(key)
    log.info(
        "Late scratching: %s R%s — %s\nsource=%s\nlocked=%s\nnew_primary=%s\nnew_backup=%s",
        venue,
        race_no,
        horse,
        source,
        str(locked).lower(),
        new_primary or "—",
        new_backup or "—",
    )
    return True


def reset_logged_transitions() -> None:
    _LOGGED_TRANSITIONS.clear()


def odds_rows_from_lookup(odds_lookup, venue: str, race_no, runners: list[Runner]) -> list[dict[str, Any]]:
    """Derive Sportsbet-style rows by querying every field runner. Missing odds ≠ scratched."""
    if odds_lookup is None:
        return []
    out: list[dict[str, Any]] = []
    for runner in runners or []:
        name = str(getattr(runner, "name", "") or "")
        if not name:
            continue
        try:
            o = odds_lookup(venue, race_no, name)
        except Exception:
            continue
        if not o:
            continue
        item = dict(o)
        item.setdefault("name", name)
        item.setdefault("source", SOURCE_SPORTSBET)
        out.append(item)
    return out
