# Parser implementation status

Summary of what each parser provides and what’s still missing.

## Fully implemented (meetings + races + runners)

| Parser | Source | Code | Meetings | Races | Runners |
|--------|--------|------|----------|-------|---------|
| **parse_thedogs** | thedogs.com.au | AU Greyhound | ✅ | ✅ | ✅ |
| **parse_harness** | harness.org.au (NSW) | AU Harness | ✅ | ✅ | ✅ |
| **parse_racingaustralia** | racingaustralia.horse | AU Thoroughbred | ✅ | ✅ | ✅ |
| **parse_hrnz_nz** | hrnz.co.nz | NZ Harness | ✅ | ✅ | ✅ |

## Partially implemented

### parse_grnz (NZ Greyhound — grnz.co.nz)

- **Meetings**: ✅ Implemented (fields/calendar page + Hatrick Straight fallback).
- **Races**: ⚠️ Placeholder only — returns R1..R12 with no real times or URLs; no fetch of GRNZ per-meeting fields.
- **Runners**: ❌ Not implemented (no runner list; field shows 0).

**To complete**: Fetch GRNZ per-meeting/fields page, parse race list (numbers, times) and runner list (box, name, etc.) and return real `Race`/`Runner` data.

### parse_nzracing (NZ Thoroughbred — nzracing.co.nz)

- **Meetings**: ✅ Implemented (nom-fields page; meeting list with venue, date, meeting URL).
- **Races**: ❌ Stubbed — `fetch_races_and_runners_for_meeting` returns `([], {}, {})`.
- **Runners**: ❌ Stubbed (same).

**To complete**: Implement race/runner parsing from nzracing.co.nz (e.g. meeting-overview or fields page) and return real `Race`/`Runner` data.

## Other modules (not “missing”, different role)

- **parse_skyracing_schedule** — Sky Racing TV schedule (channel/time overlay); not a meeting/race/runner parser.
- **parse_racingnsw** — Used by `review.py` (results) and `history.py` (horse history); AU TB meetings/races come from **parse_racingaustralia**.

---

**Summary**: The only clearly *missing* parser implementations are:

1. **GRNZ (NZ greyhound)** — Real race list + runner list from GRNZ fields pages.
2. **NZ Thoroughbred (nzracing)** — Race and runner parsing from nzracing.co.nz (meetings already work).
