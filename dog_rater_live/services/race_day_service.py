"""Build chronological thoroughbred race-day view models from loaded meetings/fields."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

from models import Meeting, Race, Runner
from parse_racingaustralia import parse_race_class_label
from services.formatting import (
    format_clock,
    format_countdown,
    hero_running_hold,
    hero_yield_before_next,
    minutes_until,
    tb_race_duration,
)
from services.runner_numbers import program_number_for_name, program_number_for_runner
from services.scratching import (
    effective_scratching_state,
    odds_rows_from_lookup,
    persisted_scratch_records,
    resolve_live_selection,
)

STATE_TZ = {
    "NSW": "Australia/Sydney",
    "ACT": "Australia/Sydney",
    "VIC": "Australia/Sydney",
    "TAS": "Australia/Sydney",
    "QLD": "Australia/Brisbane",
    "SA": "Australia/Adelaide",
    "NT": "Australia/Darwin",
    "WA": "Australia/Perth",
}

AU_STATES = ("NSW", "VIC", "QLD", "SA", "WA", "TAS")

# Prefer these when several races share a jump minute (picnic/country after metro).
_METRO_MARKERS = (
    "rosehill",
    "randwick",
    "warwick farm",
    "canterbury",
    "flemington",
    "caulfield",
    "moonee valley",
    "sandown",
    "eagle farm",
    "doomben",
    "gold coast",
    "ascot",
    "belmont",
    "morphettville",
    "murray bridge",
    "elwick",
    "hobart",
    "launceston",
)


def venue_tier(view: object) -> int:
    raw = f"{getattr(view, 'venue_raw', '')} {getattr(view, 'venue', '')}".lower()
    return 0 if any(m in raw for m in _METRO_MARKERS) else 1


def resolve_tz(name: str) -> ZoneInfo:
    if not name or name == "Local (server)":
        return datetime.now().astimezone().tzinfo  # type: ignore[return-value]
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo("Australia/Sydney")


def meeting_tz(meeting: Meeting, fallback: ZoneInfo) -> ZoneInfo:
    extra = getattr(meeting, "extra", {}) or {}
    if extra.get("country") == "NZ":
        return ZoneInfo("Pacific/Auckland")
    state = str(extra.get("state") or "").upper().strip()
    tz_name = STATE_TZ.get(state)
    if tz_name:
        return ZoneInfo(tz_name)
    return fallback


def meeting_state(meeting: Meeting) -> str:
    return str((getattr(meeting, "extra", {}) or {}).get("state") or "").upper().strip()


def is_trial_meeting(meeting: Meeting) -> bool:
    key = str((getattr(meeting, "extra", {}) or {}).get("key") or "")
    return ",Trial" in key or key.endswith("Trial")


def jump_datetime(
    *,
    chosen_date: date,
    start_time_local: Optional[time],
    meeting: Meeting,
    app_tz: ZoneInfo,
) -> Optional[datetime]:
    if not isinstance(start_time_local, time):
        return None
    local_tz = meeting_tz(meeting, app_tz)
    local_dt = datetime.combine(chosen_date, start_time_local, tzinfo=local_tz)
    return local_dt.astimezone(app_tz)


def compact_track_condition(raw: str) -> str:
    s = (raw or "").strip()
    if s.upper() in {"N/A", "NA", "-", ""}:
        return ""
    import re

    m = re.match(r"^(Firm|Good|Soft|Heavy|Synth(?:etic)?)\s*(\d+)?\b", s, re.IGNORECASE)
    if not m:
        return s[:18]
    base = m.group(1)
    if base.lower().startswith("synth"):
        return "Synth"
    return f"{base.title()}{m.group(2) or ''}"


def runner_program_number(runner: Runner) -> str:
    """Official program/saddle number as display text. Never the barrier."""
    n = program_number_for_runner(runner)
    return str(n) if n is not None else ""


def number_for_name(runners: list[Runner], name: str) -> str:
    n = program_number_for_name(runners, name)
    return str(n) if n is not None else ""


@dataclass
class RaceView:
    """Race-day row.

    ``primary`` / ``backup`` are the *current active* selections (never a scratched
    runner). ``original_primary`` / ``original_backup`` preserve the first saved
    names when a late scratching promoted a replacement.
    """

    meeting_url: str
    race_url: str
    code: str
    venue: str
    venue_raw: str
    state: str
    race_no: int
    race_name: str
    race_class: str
    distance_m: Optional[int]
    track_condition: str
    jump_at: Optional[datetime]
    status: str  # upcoming | in_progress | finished | unknown
    primary: str
    primary_no: str
    backup: str
    backup_no: str
    primary_score: Optional[float]
    backup_score: Optional[float]
    score_gap: float
    confidence_label: str
    odds: Optional[float]
    backup_odds: Optional[float]
    field_size: int
    scratching_warning: bool
    locked: bool
    from_snapshot: bool
    live_status: str
    runners: list[Runner] = field(default_factory=list)
    ranked: list = field(default_factory=list)
    why: list[str] = field(default_factory=list)
    weights: dict[str, Any] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)
    original_primary: str = ""
    original_primary_no: str = ""
    original_backup: str = ""
    original_backup_no: str = ""
    primary_scratched: bool = False
    backup_scratched: bool = False
    backup_promoted: bool = False
    scratching_sources: dict[str, list[str]] = field(default_factory=dict)
    selection_warning: str = ""
    no_active_selection: bool = False
    scratch_confirmed_at: str = ""

    @property
    def race_key(self) -> tuple[str, int]:
        return (self.meeting_url, int(self.race_no))

    @property
    def active_primary(self) -> str:
        return self.primary

    @property
    def active_backup(self) -> str:
        return self.backup

    @property
    def active_primary_no(self) -> str:
        return self.primary_no

    @property
    def active_backup_no(self) -> str:
        return self.backup_no

    def countdown(self, now: datetime) -> str:
        return format_countdown(self.jump_at, now)

    def clock(self) -> str:
        return format_clock(self.jump_at)


OddsLookup = Callable[[str, int, str], Optional[dict[str, Any]]]


def _row_status(now: datetime, jump_at: Optional[datetime]) -> str:
    if jump_at is None:
        return "unknown"
    if now < jump_at:
        return "upcoming"
    if now <= jump_at + tb_race_duration():
        return "in_progress"
    return "finished"


def live_status(now: datetime, jump_at: Optional[datetime]) -> str:
    return _row_status(now, jump_at)


def chronological_sort_key(row: RaceView) -> tuple:
    if row.jump_at is None:
        return (1, 0.0, 1, row.venue, row.race_no)
    return (0, row.jump_at.timestamp(), venue_tier(row), row.venue, row.race_no)


def _attach_active_odds(row: RaceView, odds_lookup: Optional[OddsLookup]) -> None:
    if not odds_lookup:
        return
    if row.primary:
        o = odds_lookup(row.venue_raw or row.venue, row.race_no, row.primary)
        if o and not o.get("scratched") and row.odds is None:
            row.odds = o.get("win")
    if row.backup:
        b = odds_lookup(row.venue_raw or row.venue, row.race_no, row.backup)
        if b and not b.get("scratched") and row.backup_odds is None:
            row.backup_odds = b.get("win")


def build_race_views(
    *,
    chosen_date: date,
    meetings: list[Meeting],
    fields_by_meeting: dict[str, dict],
    now: datetime,
    app_tz: ZoneInfo,
    state_filter: str = "All",
    code: str = "thoroughbred",
    saved_picks: Optional[dict[tuple[str, int], dict]] = None,
    odds_lookup: Optional[OddsLookup] = None,
    odds_rows_lookup: Optional[Callable[[str, int], list[dict[str, Any]]]] = None,
    rank_upcoming_only: bool = False,
    upcoming_rank_limit: int = 12,
) -> list[RaceView]:
    saved_picks = saved_picks or {}
    want_state = (state_filter or "All").upper()
    views: list[RaceView] = []

    for m in meetings:
        if getattr(m, "code", "") != code:
            continue
        if is_trial_meeting(m):
            continue
        state = meeting_state(m)
        if want_state not in {"", "ALL"} and state != want_state:
            continue
        mf = fields_by_meeting.get(getattr(m, "meeting_url", ""), {}) or {}
        races: list[Race] = mf.get("races") or []
        runners_by = mf.get("runners_by_race") or {}
        meta = mf.get("meta") or {}
        track = compact_track_condition(str(meta.get("track_condition") or ""))
        venue_raw = getattr(m, "venue", "") or ""
        venue = f"{venue_raw} ({state})" if state else venue_raw

        for r in races:
            rn = getattr(r, "race_no", None)
            try:
                race_no = int(rn)
            except (TypeError, ValueError):
                continue
            jump_at = jump_datetime(
                chosen_date=chosen_date,
                start_time_local=getattr(r, "start_time_local", None),
                meeting=m,
                app_tz=app_tz,
            )
            runners = list(runners_by.get(race_no) or runners_by.get(str(race_no)) or [])
            race_name = str(getattr(r, "name", "") or "")
            class_label = str((getattr(r, "extra", {}) or {}).get("class_label") or "") or parse_race_class_label(
                race_name
            )
            live_status = _row_status(now, jump_at)
            views.append(
                RaceView(
                    meeting_url=getattr(m, "meeting_url", "") or "",
                    race_url=str(getattr(r, "race_url", "") or ""),
                    code=code,
                    venue=venue,
                    venue_raw=venue_raw,
                    state=state,
                    race_no=race_no,
                    race_name=race_name,
                    race_class=class_label,
                    distance_m=getattr(r, "distance_m", None),
                    track_condition=track,
                    jump_at=jump_at,
                    status=live_status,
                    primary="",
                    primary_no="",
                    backup="",
                    backup_no="",
                    primary_score=None,
                    backup_score=None,
                    score_gap=0.0,
                    confidence_label="",
                    odds=None,
                    backup_odds=None,
                    field_size=sum(1 for x in runners if not bool(getattr(x, "scratched", False))),
                    scratching_warning=False,
                    locked=False,
                    from_snapshot=False,
                    live_status=live_status,
                    runners=runners,
                    meta=meta,
                )
            )

    views.sort(key=chronological_sort_key)

    upcoming_ranked = 0
    for row in views:
        saved = saved_picks.get(row.race_key)
        locked = bool(saved and saved.get("locked"))
        odds_rows: list[dict[str, Any]] = []
        if odds_rows_lookup:
            try:
                odds_rows = list(odds_rows_lookup(row.venue_raw or row.venue, row.race_no) or [])
            except Exception:
                odds_rows = []
        if not odds_rows and odds_lookup:
            odds_rows = odds_rows_from_lookup(odds_lookup, row.venue_raw or row.venue, row.race_no, row.runners)
        effective = effective_scratching_state(
            row.runners,
            odds_rows,
            persisted_scratch_records(saved),
            venue=row.venue_raw or row.venue,
            race_no=row.race_no,
            now=now,
        )
        row.runners = effective.runners
        row.field_size = effective.field_size
        row.scratching_sources = {name: list(rec.sources) for name, rec in effective.records.items() if rec.scratched}

        should_rank = True
        if rank_upcoming_only:
            should_rank = row.status == "upcoming" and upcoming_ranked < upcoming_rank_limit
        phase = row.status
        use_snapshot_only = bool(saved) and (locked or phase != "upcoming")
        if should_rank or use_snapshot_only:
            resolved = resolve_live_selection(
                effective=effective,
                saved=saved,
                phase=phase,
                locked=locked,
                track_condition=row.meta.get("track_condition"),
            )
            row.ranked = resolved.ranked
            # Active names — never a scratched runner.
            row.primary = resolved.active_primary
            row.backup = resolved.active_backup
            row.primary_no = resolved.active_primary_no
            row.backup_no = resolved.active_backup_no
            row.original_primary = resolved.original_primary
            row.original_primary_no = resolved.original_primary_no
            row.original_backup = resolved.original_backup
            row.original_backup_no = resolved.original_backup_no
            row.primary_scratched = resolved.primary_scratched
            row.backup_scratched = resolved.backup_scratched
            row.backup_promoted = resolved.backup_promoted
            row.no_active_selection = resolved.no_active_selection
            row.selection_warning = resolved.selection_warning
            row.primary_score = resolved.primary_score
            row.backup_score = resolved.backup_score
            row.score_gap = resolved.score_gap
            row.confidence_label = resolved.confidence_label
            row.why = resolved.why
            row.weights = {"draw": resolved.weights[0], "form": resolved.weights[1], "proxy": resolved.weights[2], "auto": True}
            row.from_snapshot = resolved.from_snapshot
            row.locked = locked
            if resolved.from_snapshot and saved and not resolved.backup_promoted and not resolved.primary_scratched:
                if row.odds is None:
                    row.odds = saved.get("primary_odds")
                if row.backup_odds is None:
                    row.backup_odds = saved.get("backup_odds")
            if row.status == "upcoming" and not resolved.from_snapshot:
                upcoming_ranked += 1
        elif saved:
            row.from_snapshot = True
            row.locked = locked
            row.original_primary = str(saved.get("original_primary") or saved.get("pick_name") or "")
            row.original_backup = str(saved.get("original_backup") or saved.get("backup") or "")
            row.primary = row.original_primary
            row.backup = row.original_backup

        if saved and saved.get("scratching_detected_at"):
            row.scratch_confirmed_at = str(saved.get("scratching_detected_at") or "")

        _attach_active_odds(row, odds_lookup)

        if row.primary and effective.is_scratched(row.primary):
            # Invariant: never leave a scratched horse as the active primary.
            row.primary = ""
            row.primary_no = ""
            row.no_active_selection = True
            row.selection_warning = row.selection_warning or "NO ACTIVE SELECTION"
        if row.backup and effective.is_scratched(row.backup):
            row.backup = ""
            row.backup_no = ""

        row.scratching_warning = bool(
            row.primary_scratched or row.backup_scratched or row.selection_warning or row.no_active_selection
        )

    return views


def _on_hero_card(row: RaceView, now: datetime) -> bool:
    if row.jump_at is None:
        return False
    if row.jump_at > now:
        return True
    return now <= row.jump_at + hero_running_hold()


def _next_is_imminent(upcoming: list[RaceView], now: datetime, current: RaceView) -> bool:
    cutoff = now + hero_yield_before_next()
    for row in upcoming:
        if row.race_key == current.race_key or row.jump_at is None:
            continue
        if row.jump_at <= cutoff:
            return True
    return False


def next_to_jump(rows: list[RaceView], now: datetime, sticky: Optional[RaceView] = None) -> Optional[RaceView]:
    """Soonest race to track from `rows` at `now`.

    When future races exist, the hero is the chronologically earliest (metro
    meetings win same-instant ties). A race that has already jumped stays on
    the card only while it is still in the running window, and only if the
    next race is not about to jump. Sticky is that live-race rule — it must
    not pin a later *upcoming* meeting over an earlier one that arrived later.
    """
    upcoming = [r for r in rows if r.jump_at is not None and r.jump_at > now]
    upcoming.sort(key=lambda r: (r.jump_at or now, venue_tier(r), r.venue, r.race_no))

    if sticky is not None:
        live_sticky = next((r for r in rows if r.race_key == sticky.race_key), None)
        jumped = (
            live_sticky is not None
            and live_sticky.jump_at is not None
            and live_sticky.jump_at <= now
            and _on_hero_card(live_sticky, now)
        )
        if jumped and not _next_is_imminent(upcoming, now, live_sticky):
            return live_sticky

    running = [
        r
        for r in rows
        if r.jump_at is not None and r.jump_at <= now <= r.jump_at + hero_running_hold()
    ]
    if running:
        running.sort(key=lambda r: (-(r.jump_at or now).timestamp(), venue_tier(r), r.venue))
        candidate = running[0]
        if not _next_is_imminent(upcoming, now, candidate):
            return candidate

    if upcoming:
        return upcoming[0]
    return None


@dataclass
class RaceDayState:
    """Hero + upcoming derived from one `views` list and one `now`."""

    now: datetime
    hero: Optional[RaceView]
    upcoming: list[RaceView]


def derive_race_day_state(
    rows: list[RaceView],
    now: datetime,
    *,
    sticky: Optional[RaceView] = None,
    limit: int = 12,
) -> RaceDayState:
    hero = next_to_jump(rows, now, sticky=sticky)
    upcoming = [r for r in rows if r.jump_at is not None and r.jump_at > now]
    upcoming.sort(key=lambda r: (r.jump_at or now, venue_tier(r), r.venue, r.race_no))
    rest = [r for r in upcoming if hero is None or r.race_key != hero.race_key]
    return RaceDayState(now=now, hero=hero, upcoming=rest[: max(0, int(limit))])


def upcoming_races(
    rows: list[RaceView],
    now: datetime,
    limit: int = 8,
    sticky: Optional[RaceView] = None,
) -> list[RaceView]:
    return derive_race_day_state(rows, now, sticky=sticky, limit=limit).upcoming


def urgency_color(row: RaceView, now: datetime) -> str:
    """Sparse colour token: amber / red / grey / green / ''."""
    if row.scratching_warning:
        return "red"
    if row.status == "finished":
        return "grey"
    mins = minutes_until(row.jump_at, now)
    if mins is not None and 0 < mins < 5:
        return "amber"
    return ""
