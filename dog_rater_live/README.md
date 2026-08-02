# dog_rater_live

Streamlit app that pulls near‑realtime Australian (and some NZ) race fields from public websites, ranks runners, and explains **why this runner could win**. Supports greyhounds, thoroughbreds, and harness.

## Disclaimer

- **For fun / modelling only.** Not betting advice.
- Uses **public, free websites** only — no paid APIs, odds feeds, or auth.
- Weather/conditions are informational and may be incomplete.

---

## Data sources

| Code | Source | Notes |
|------|--------|------|
| **Greyhounds (AU)** | thedogs.com.au | Fields + racecards; RAS fallback (best-effort). |
| **Thoroughbred (AU)** | racingaustralia.horse | Calendar, Acceptances, Race Program. Times in meeting local time; app converts to selected timezone. |
| **Harness (NSW)** | harness.org.au | Fields + form (NSW). |
| **Harness (NZ)** | hrnz.co.nz | HRNZ fields index + meeting pages. |
| **Greyhounds (NZ)** | GRNZ / Hatrick Straight | Meetings + fallback when fetch fails; runner parsing best-effort. |
| **Thoroughbred (NZ)** | nzracing.co.nz | Meetings only (race/runner parsing stubbed). |

Weather (optional) comes from a public API (e.g. Open-Meteo) for venue context only.

See **PARSERS.md** for implementation status per parser.

---

## Requirements

- Python **3.10+**

## Install

From the **repo root** (or from `dog_rater_live`):

```bash
cd dog_rater_live
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Run

**Streamlit (recommended):**

```bash
streamlit run app.py
```

Or use the startup script (creates venv, installs deps, runs Streamlit):

```bash
./start.sh
```

---

## UI overview

- **Code**: Greyhounds, Thoroughbred (AU), Harness (NSW), **All (AU)**, Greyhounds (NZ), Harness (NZ), Thoroughbred (NZ), **All (AU+NZ)**.
- **Timezone**: e.g. Australia/Sydney, Pacific/Auckland.
- **Roster**: Next-to-jump grid; type filter **All | Thoroughbred | Harness | Greyhound**; optional “only next per venue”, “show finished” (default off), Sky overlay (default on).
- **Daily review**: Compare saved picks to fetched results (winner only; best-effort).
- **Compression backtest (TB)**: Measure whether small score gaps (Rank 1 vs 2/3) correlate with place-heavy outcomes; run over a date range and see win/place rates by clustered vs clear-edge.

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

- `app.py` — Streamlit app (roster, picks, review, compression backtest).
- `scoring.py` — Runner ranking (weights, form, draw, conditions).
- `review.py` — Fetch results (winners, place getters for TB) for daily review and backtest.
- `backtest_compression.py` — Compression index backtest and report.
- `parse_*.py` — Parsers for each code/source (see PARSERS.md).
- `journal.py` — Save/load picks for daily review.
- `db_cache.py` — SQLite cache for parsed meetings/fields (persists across app restarts; `cache/roster.db`).
- `race_db.py` — Persistent daily race data, picks, and results in the same DB: load/save fields by date, "Update race" to refresh one race, store picks (roster + journal), and persist results so Daily review can match picks to winners/place without re-fetching.
