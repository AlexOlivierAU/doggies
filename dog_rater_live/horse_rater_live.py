from __future__ import annotations

import argparse
from datetime import date

from parse_harness import fetch_meetings_for_date as fetch_harness_meetings
from parse_harness import fetch_races_and_runners_for_meeting as fetch_harness_fields
from parse_racingaustralia import fetch_meetings_for_date as fetch_tb_meetings
from parse_racingaustralia import fetch_races_and_runners_for_meeting as fetch_tb_fields
from scoring import rank_runners
from history import history_bullets_for_runner


def _parse_date(s: str) -> date:
    return date.fromisoformat(s)


def main() -> int:
    p = argparse.ArgumentParser(
        description="horse_rater_live (CLI): rank runners (for fun, not betting advice).",
    )
    p.add_argument("--code", choices=["thoroughbred", "harness"], required=True)
    p.add_argument("--date", default=date.today().isoformat(), help="YYYY-MM-DD")
    p.add_argument("--venue", required=True, help="Venue substring to match (case-insensitive).")
    p.add_argument("--race", type=int, required=True, help="Race number.")
    p.add_argument("--draw-w", type=float, default=0.33, help="Inside draw weight (0-1).")
    p.add_argument("--form-w", type=float, default=0.34, help="Recent form weight (0-1).")
    p.add_argument("--class-w", type=float, default=0.33, help="Class/weight proxy weight (0-1).")
    p.add_argument("--top", type=int, default=5)
    p.add_argument("--history", action="store_true", help="Include best-effort historical snippets (may be slower).")
    args = p.parse_args()

    d = _parse_date(args.date)

    if args.code == "thoroughbred":
        meetings = fetch_tb_meetings(d)
    else:
        meetings = fetch_harness_meetings(d)

    if not meetings:
        print(f"No meetings found for {d} ({args.code}).")
        return 2

    wanted = [m for m in meetings if args.venue.lower() in m.venue.lower()]
    if not wanted:
        print("No matching venues. Available:")
        for m in meetings:
            print(f"- {m.venue}")
        return 2
    meeting = wanted[0]

    if args.code == "thoroughbred":
        races, runners_by, _meta = fetch_tb_fields(meeting.meeting_url)
    else:
        races, runners_by = fetch_harness_fields(meeting.meeting_url, meeting.meeting_date)

    race = next((r for r in races if r.race_no == args.race), None)
    if not race:
        print(f"Race {args.race} not found. Available: {[r.race_no for r in races]}")
        return 2

    runners = runners_by.get(args.race, [])
    ranked = rank_runners(
        runners,
        box_weight=args.draw_w,
        form_weight=args.form_w,
        early_weight=args.class_w,
        explain_mode="short",
    )

    print(f"{args.code.upper()} | {meeting.venue} {meeting.meeting_date} | Race {race.race_no}: {race.name}")
    print("For fun / exploratory only. Not betting advice.\n")
    for rr in ranked[: args.top]:
        dlabel = rr.draw_label
        dval = rr.draw if rr.draw is not None else "?"
        print(f"{rr.rank:>2}. {rr.name} ({dlabel} {dval}) score={rr.score:.3f} factors={rr.key_factors}")
        for b in rr.why_bullets:
            print(f"    - {b}")
        if args.history:
            r_obj = next((r for r in runners if getattr(r, "name", None) == rr.name), None)
            if r_obj is not None:
                hb = history_bullets_for_runner(r_obj)
                for h in hb[:6]:
                    print(f"    - history: {h}")
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

