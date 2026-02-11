from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import date

from parse_thedogs import fetch_meetings_for_date, fetch_races_for_meeting, fetch_runners_for_race
from scoring import rank_runners
from history import history_bullets_for_runner


def _parse_date(s: str) -> date:
    return date.fromisoformat(s)


def main() -> int:
    p = argparse.ArgumentParser(description="dog_rater_live (CLI): rank greyhound runners (for fun, not betting advice).")
    p.add_argument("--date", default=date.today().isoformat(), help="Meeting date (YYYY-MM-DD). Default: today.")
    p.add_argument("--venue", required=True, help="Venue substring to match (case-insensitive).")
    p.add_argument("--race", type=int, required=True, help="Race number (e.g. 1).")
    p.add_argument("--box-w", type=float, default=0.33, help="Box bias weight (0-1).")
    p.add_argument("--form-w", type=float, default=0.34, help="Recent form weight (0-1).")
    p.add_argument("--early-w", type=float, default=0.33, help="Early speed proxy weight (0-1).")
    p.add_argument("--top", type=int, default=5, help="How many to print.")
    p.add_argument("--debug", action="store_true", help="Print debug info.")
    p.add_argument("--history", action="store_true", help="Include best-effort historical snippets (may be slower).")
    args = p.parse_args()

    d = _parse_date(args.date)
    meetings = fetch_meetings_for_date(d)
    if not meetings:
        print(f"No meetings found for {d} from the primary source.")
        return 2

    wanted = [m for m in meetings if args.venue.lower() in m.venue.lower()]
    if not wanted:
        print("No matching venues. Available venues:")
        for m in meetings:
            print(f"- {m.venue} ({m.meeting_url})")
        return 2
    meeting = wanted[0]

    races = fetch_races_for_meeting(meeting.meeting_url)
    race = next((r for r in races if r.race_no == args.race), None)
    if race is None:
        print(f"Race {args.race} not found. Available races: {[r.race_no for r in races]}")
        return 2

    runners = fetch_runners_for_race(race.race_url)
    ranked = rank_runners(
        runners,
        box_weight=args.box_w,
        form_weight=args.form_w,
        early_weight=args.early_w,
        explain_mode="short",
    )

    print(f"Meeting: {meeting.venue} {meeting.meeting_date} | Race {race.race_no}: {race.name}")
    print("For fun / exploratory only. Not betting advice.\n")
    for rr in ranked[: args.top]:
        box = rr.draw if rr.draw is not None else "?"
        print(f"{rr.rank:>2}. {rr.name} (Box {box})  score={rr.score:.3f}  factors={rr.key_factors}")
        for b in rr.why_bullets:
            print(f"    - {b}")
        if args.history:
            r_obj = next((r for r in runners if getattr(r, "name", None) == rr.name), None)
            if r_obj is not None:
                hb = history_bullets_for_runner(r_obj)
                for h in hb[:6]:
                    print(f"    - history: {h}")
        if args.debug:
            print(f"    debug={asdict(rr.debug)}")
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

