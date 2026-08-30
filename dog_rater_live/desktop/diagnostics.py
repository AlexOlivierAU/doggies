"""Live card-loader smoke test without starting Qt.

    python -m desktop.diagnostics --date today
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime
from pathlib import Path

log = logging.getLogger("race_day_rater.diagnostics")


def _parse_date(value: str) -> date:
    raw = (value or "today").strip().lower()
    if raw in {"today", "now"}:
        return date.today()
    return date.fromisoformat(value)


def _sanitize(text: str) -> str:
    text = " ".join(str(text or "").split())
    if len(text) > 120:
        text = text[:117] + "..."
    lower = text.lower()
    if "cookie" in lower or "authorization" in lower or "password" in lower:
        return "(redacted)"
    return text


def _error_line(failed_states: list[str], errors: list[str]) -> str:
    parts: list[str] = []
    for st in failed_states or []:
        parts.append(f"{st} calendar timeout")
    for err in errors or []:
        if any(st in err for st in (failed_states or [])):
            continue
        parts.append(_sanitize(err))
    return "; ".join(parts) if parts else "none"


def run_diagnostics(chosen_date: date, *, live: bool = True, db_path: Path | None = None) -> dict:
    from desktop.paths import shared_default_db_path
    from race_db import load_daily_fields
    from services.card_loader import MEETINGS_CODE, load_cached_card, refresh_card
    from services.race_day_service import build_race_views, resolve_tz, upcoming_races

    db = Path(db_path) if db_path is not None else shared_default_db_path()
    cached_meetings, cached_fields = load_cached_card(chosen_date, db, MEETINGS_CODE)
    payload = refresh_card(
        chosen_date,
        previous_meetings=cached_meetings,
        previous_fields=cached_fields,
        db_path=db,
        live=live,
        force=False,
    )
    meetings = payload.meetings or cached_meetings
    fields = payload.fields_by_meeting or cached_fields
    app_tz = resolve_tz("Australia/Sydney")
    now = datetime.now(app_tz)
    views = build_race_views(
        chosen_date=chosen_date,
        meetings=meetings,
        fields_by_meeting=fields,
        now=now,
        app_tz=app_tz,
        rank_upcoming_only=False,
    )
    upcoming = upcoming_races(views, now, limit=40)
    with_fields = 0
    races = 0
    with_runners = 0
    for m in meetings:
        url = getattr(m, "meeting_url", "") or ""
        mf = fields.get(url) or {}
        if mf.get("races") or mf.get("runners_by_race"):
            with_fields += 1
        races += len(mf.get("races") or [])
        runners_by = mf.get("runners_by_race") or {}
        with_runners += sum(1 for rn, rs in runners_by.items() if rs)
        if not mf and url:
            stored = load_daily_fields(chosen_date, url, db_path=db)
            if stored:
                with_fields += 1
    failed = list(getattr(payload, "failed_states", None) or [])
    return {
        "database": str(db.resolve() if db.exists() else db),
        "cached_meetings": len(cached_meetings),
        "live_meetings": len(payload.meetings) if live else 0,
        "meetings_with_fields": with_fields,
        "races": races or len(views),
        "races_with_runners": with_runners or sum(1 for v in views if v.runners),
        "views_built": len(views),
        "upcoming_views": len(upcoming),
        "errors": _error_line(failed, list(payload.errors or [])),
        "status": payload.status,
        "message": payload.message,
    }


def format_report(stats: dict) -> str:
    return "\n".join(
        [
            f"Database: {stats['database']}",
            f"Cached meetings: {stats['cached_meetings']}",
            f"Live meetings: {stats['live_meetings']}",
            f"Meetings with fields: {stats['meetings_with_fields']}",
            f"Races: {stats['races']}",
            f"Races with runners: {stats['races_with_runners']}",
            f"Views built: {stats['views_built']}",
            f"Upcoming views: {stats['upcoming_views']}",
            f"Errors: {stats['errors']}",
        ]
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Race Day desktop card diagnostics (no Qt).")
    parser.add_argument("--date", default="today", help="YYYY-MM-DD or 'today'")
    parser.add_argument("--offline", action="store_true", help="Use SQLite cache only (no live fetches).")
    parser.add_argument("--db", default="", help="Override SQLite path.")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    chosen = _parse_date(args.date)
    db = Path(args.db).expanduser() if args.db else None
    try:
        stats = run_diagnostics(chosen, live=not args.offline, db_path=db)
    except Exception as exc:
        log.exception("Diagnostics failed")
        print(f"Errors: {_sanitize(str(exc))}", file=sys.stderr)
        return 1
    print(format_report(stats))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
