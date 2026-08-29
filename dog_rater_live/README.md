# dog_rater_live

Thoroughbred-first race-day dashboard. The app pulls public Australian (and some NZ) race fields, ranks runners with **adaptive heuristic weights**, saves immutable pick snapshots, and matches those snapshots to published results.

Greyhound and harness sources remain available under **Settings**, but they are not part of the default Race Day view.

## Disclaimer

- **For fun / modelling only.** Not betting advice.
- Uses **public, free websites** only — no paid APIs, odds feeds, or auth.
- Weather/conditions are informational and may be incomplete.
- Confidence labels (Strong / Medium / Close race) are derived from the model score gap. They are **not** statistically validated probabilities.
- Win/place strike rates are descriptive counts of saved snapshots vs confirmed results. They are not a profitability claim.

---

## Screenshot

Add a Race Day screenshot here when one is available:

`docs/race-day.png` (placeholder)

---

## Data sources

| Code | Source | Notes |
|------|--------|------|
| **Thoroughbred (AU)** | racingaustralia.horse | Calendar, Acceptances, Race Program. Times in meeting local time; app converts to the selected timezone. |
| **Odds / late scratchings** | Sportsbet public racing API | Best-effort win odds and fluctuations. Not TAB. |
| **Greyhounds (AU)** | thedogs.com.au | Fields + racecards; RAS fallback (best-effort). |
| **Harness (NSW)** | harness.org.au | Fields + form (NSW). |
| **Harness (NZ)** | hrnz.co.nz | HRNZ fields index + meeting pages. |
| **Greyhounds (NZ)** | GRNZ / Hatrick Straight | Meetings + fallback; runner parsing best-effort. |
| **Thoroughbred (NZ)** | nzracing.co.nz | Meetings only (race/runner parsing stubbed). |

Weather (optional) comes from a public API (e.g. Open-Meteo) for venue context only.

See **PARSERS.md** for implementation status per parser.

**Limitations:** parsers miss late changes; result pages can lag; runner-name matching is exact after normalisation (country suffixes stripped) and will not silently assign a result from a fuzzy match.

---

## Requirements

- Python **3.10+**

## Install

From `dog_rater_live`:

```bash
cd dog_rater_live
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

For tests and lint:

```bash
pip install -r requirements-dev.txt
```

## Run

**Streamlit (recommended):**

```bash
streamlit run app.py
```

Or:

```bash
./start.sh
```

The default screen is **Race Day** (thoroughbreds).

---

## Navigation

| Section | Purpose |
|---------|---------|
| **Race Day** | Next-to-jump hero, upcoming TB races, today's saved picks and results. |
| **Race Details** | Ranked field for one race (compact rows; score components collapsed). |
| **History** | Saved snapshots over a date range — never a live re-rank of an old race. |
| **Model** | Weight controls, auto-weight rationale, compression backtest. |
| **Settings** | Timezone, refresh, data-source code, database, diagnostics, full legacy roster (incl. greyhound/harness). |

## Race Day workflow

1. Date defaults to today in the selected Australian timezone (change timezone in Settings).
2. Filter by state if you only want NSW/VIC/QLD/SA/WA/TAS.
3. The **hero card** is the next thoroughbred race: venue, jump time, countdown, primary pick, backup, confidence, scratching warning.
4. **Confirm / save pick** locks an immutable snapshot. Picks are also auto-locked shortly before jump.
5. **Upcoming** lists the next several races in chronological jump order. Finished races do not dominate this table.
6. **Today's picks** shows saved snapshots with statuses (`PENDING`, `AWAITING RESULT`, `WIN`, `PLACED`, `LOST`, `PRIMARY SCRATCHED`, `BACKUP WON`, `VOID`, `RESULT UNAVAILABLE`) and 1st/2nd/3rd labels.
7. Daily win/place strike rates use completed primary results only. Estimated $1 win-only return is shown only when every completed pick has a saved decimal price.

## Saved pick snapshots

When a pick is confirmed or auto-locked near jump, the app stores:

- race identity, venue, race number, scheduled jump time
- primary and backup names, scores, score gap / confidence label
- odds at selection time (when the odds feed is available)
- timestamp, model weights, field snapshot, scratching flags

Later live ranks **do not overwrite** a locked snapshot. If the primary is scratched, the original name is kept and `backup_promoted` is recorded.

## Result resolution

After a race jumps:

1. Status becomes **AWAITING RESULT**.
2. Public result pages are fetched and stored (winner / 2nd / 3rd).
3. Saved primary and backup names are matched after normalisation.
4. Race Day and History update.
5. Empty results are retried later (still awaiting).
6. Fetch errors and unmatchable names become **RESULT UNAVAILABLE** (logged), not a silent win/loss.

## Testing

From `dog_rater_live`:

```bash
pytest -q
ruff check services ui tests
python -m compileall -q .
```

Tests use synthetic data and a temporary SQLite file. They do not hit live racing websites.

GitHub Actions runs compile, Ruff, and Pytest on `main` and pull requests.

---

## CLI

**Greyhounds:**

```bash
python dog_rater_live.py --date 2026-02-06 --venue "Wentworth" --race 1
```

**Thoroughbred (AU):**

```bash
python horse_rater_live.py --code thoroughbred --date 2026-02-07 --venue "Caulfield" --race 1
```

**Harness (NSW):**

```bash
python horse_rater_live.py --code harness --date 2026-02-06 --venue "Newcastle" --race 1
```

**Compression backtest (TB, last 7 days):**

```bash
python -m backtest_compression
```

---

## Repo layout

- `app.py` — Streamlit entry: session load, navigation, existing fetch/cache helpers, legacy roster grid.
- `ui/` — Race Day, Race Details, History, Model, Settings.
- `services/` — confidence labels, pick snapshots, result matching, race-day view models.
- `scoring.py` — Runner ranking (heuristic weights, form, draw, conditions).
- `review.py` — Fetch results (winners, place getters for TB).
- `backtest_compression.py` — Compression index backtest.
- `parse_*.py` — Parsers (see PARSERS.md).
- `journal.py` — JSON pick journal (still merged with SQLite on load).
- `db_cache.py` — SQLite HTTP/parse cache (`cache/roster.db`).
- `race_db.py` — Daily fields, picks (including locked snapshots), results, jockey rides.
- `tests/` — Deterministic domain tests.
