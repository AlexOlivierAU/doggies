"""Build chronological thoroughbred race-day view models from loaded meetings/fields."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

from models import Meeting, Race, Runner
from parse_racingaustralia import parse_race_class_label
from services.formatting import format_clock, format_countdown, minutes_until, tb_race_duration
from services.ranking import rank_field, selections_from_ranked

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
    raw = getattr(runner, "raw", {}) or {}
    headers = raw.get("headers") or []
    cells = raw.get("cells") or []
    hn = [str(h).strip().lower() for h in headers]
    idx = None
    for i, h in enumerate(hn):
        if h in {"no.", "no", "number", "saddle", "program", "#", "num", "runner no", "runner no."}:
            idx = i
            break
    if idx is not None and 0 <= idx < len(cells):
        import re

        m = re.search(r"\b(\d{1,2})\b", str(cells[idx]))
        if m:
            return m.group(1)
    draw = getattr(runner, "draw", None)
    return str(draw) if draw is not None else ""


def number_for_name(runners: list[Runner], name: str) -> str:
    r = next((x for x in runners if getattr(x, "name", None) == name), None)
    if r is None:
        return ""
    return runner_program_number(r)


@dataclass
class RaceView:
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

    @property
    def race_key(self) -> tuple[str, int]:
        return (self.meeting_url, int(self.race_no))

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


def chronological_sort_key(row: RaceView) -> tuple:
    if row.jump_at is None:
        return (1, 0.0, row.venue, row.race_no)
    return (0, row.jump_at.timestamp(), row.venue, row.race_no)


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
            field_size = sum(1 for x in runners if not bool(getattr(x, "scratched", False)))
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
                    field_size=field_size,
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
        use_snapshot = bool(saved and (saved.get("locked") or row.status != "upcoming"))
        should_rank = True
        if rank_upcoming_only:
            should_rank = row.status == "upcoming" and upcoming_ranked < upcoming_rank_limit
        if use_snapshot and saved:
            row.from_snapshot = True
            row.locked = bool(saved.get("locked"))
            row.primary = str(saved.get("original_primary") or saved.get("pick_name") or "")
            row.backup = str(saved.get("backup") or "")
            row.primary_score = saved.get("pick_score")
            cond = saved.get("conditions") or {}
            row.backup_score = cond.get("backup_score") if isinstance(cond, dict) else saved.get("backup_score")
            row.score_gap = float(saved.get("score_gap") or 0.0)
            row.confidence_label = str(saved.get("confidence_label") or "")
            row.odds = saved.get("primary_odds")
            row.backup_odds = saved.get("backup_odds")
            row.primary_no = number_for_name(row.runners, row.primary)
            row.backup_no = number_for_name(row.runners, row.backup)
            row.why = list(saved.get("why_bullets") or [])
            row.weights = dict(saved.get("weights") or {})
            if saved.get("primary_scratched"):
                row.scratching_warning = True
        elif should_rank and row.runners:
            ranked, weights, _rationale = rank_field(
                row.runners, track_condition=row.meta.get("track_condition")
            )
            sel = selections_from_ranked(ranked)
            row.ranked = ranked
            row.primary = sel["primary"]
            row.backup = sel["backup"]
            row.primary_score = sel["primary_score"]
            row.backup_score = sel["backup_score"]
            row.score_gap = sel["score_gap"]
            row.confidence_label = sel["confidence_label"]
            row.primary_no = number_for_name(row.runners, row.primary)
            row.backup_no = number_for_name(row.runners, row.backup)
            row.why = sel["primary_why"]
            row.weights = {"draw": weights[0], "form": weights[1], "proxy": weights[2], "auto": True}
            if row.status == "upcoming":
                upcoming_ranked += 1

        if odds_lookup and row.primary:
            o = odds_lookup(row.venue_raw or row.venue, row.race_no, row.primary)
            if o and not o.get("scratched") and row.odds is None:
                row.odds = o.get("win")
            if row.backup:
                b = odds_lookup(row.venue_raw or row.venue, row.race_no, row.backup)
                if b and not b.get("scratched") and row.backup_odds is None:
                    row.backup_odds = b.get("win")

        for runner in row.runners:
            if not bool(getattr(runner, "scratched", False)):
                continue
            n = str(getattr(runner, "name", "") or "")
            if n and n in {row.primary, row.backup}:
                row.scratching_warning = True

    return views


def next_to_jump(rows: list[RaceView], now: datetime) -> Optional[RaceView]:
    upcoming = [r for r in rows if r.jump_at is not None and r.jump_at > now]
    if not upcoming:
        live = [
            r
            for r in rows
            if r.jump_at is not None and r.jump_at <= now <= r.jump_at + timedelta(minutes=8)
        ]
        if live:
            return min(live, key=lambda r: r.jump_at or now)
        return None
    return min(upcoming, key=lambda r: r.jump_at or now)


def upcoming_races(rows: list[RaceView], now: datetime, limit: int = 8) -> list[RaceView]:
    nxt = next_to_jump(rows, now)
    upcoming = [r for r in rows if r.jump_at is not None and r.jump_at > now]
    upcoming.sort(key=lambda r: r.jump_at or now)
    # Keep the hero race out of the compact table when possible.
    rest = [r for r in upcoming if nxt is None or r.race_key != nxt.race_key]
    return rest[: max(0, int(limit))]


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
