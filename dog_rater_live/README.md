# dog_rater_live

`dog_rater_live` is a fun, exploratory Streamlit app that pulls near‑realtime Australian race field/card information from public websites (greyhounds + horses), ranks runners by plausibility, and explains **why this runner could win**.

## Disclaimer

- This project is **for fun / modelling only**.
- It is **NOT betting advice**.
- It uses **public, free websites** and does not use paid APIs, odds feeds, authentication, or external services.
- Any weather/conditions displayed are **informational only** and may be incomplete or inaccurate.

## Data sources

### Greyhounds

- Primary: `https://www.thedogs.com.au/racing` (Fields / racecards + meeting/race pages)
- Fallback: `https://www.racingandsports.com.au/form-guide/greyhound` (best-effort; often blocked by bot protection)

### Thoroughbreds (All AU)

- Racing Australia FreeFields (public pages, best-effort scraping):
  - Calendar per state: `https://www.racingaustralia.horse/FreeFields/Calendar.aspx?State=...`
  - Acceptances / runners: `https://www.racingaustralia.horse/FreeFields/Acceptances.aspx?Key=YYYYMonDD,STATE,VENUE`
  - Race program (often has reliable race times): `https://www.racingaustralia.horse/FreeFields/RaceProgram.aspx?Key=...`

Notes:
- Racing Australia displays times in the **local time of the meeting**; the app converts these to your selected app timezone
  (default `Australia/Sydney`) so races can be ordered correctly across Australia.
- Some meetings may appear on the calendar with only “Weights/Program” links; the app still includes them and will prefer
  Acceptances when available.

### Harness (NSW)

- Australian Harness Racing fields + form (`https://www.harness.org.au/nsw-fields-index.cfm`, `form.cfm?mc=...`)

### New Zealand

- **Harness (NZ)**: HRNZ fields index and meeting pages (`https://infohorse.hrnz.co.nz/datahrs/fields/fields.htm`).
- **Greyhounds (NZ)** and **Thoroughbred (NZ)**: stubs (no parser yet); selectable in UI for future use.
- **All (AU+NZ)**: unified Next-to-Jump grid across AU + NZ; NZ times use `Pacific/Auckland`.

### Weather (optional)

- The app can optionally show **external live weather** for some venues using a public endpoint (currently Open‑Meteo).
- This is for context only and is **not a predictive model**.

## Requirements

- Python **3.10+**

## Install

```bash
cd dog_rater_live
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run (Streamlit)

```bash
streamlit run app.py
```

## Key UI features

- **Code selector**: Greyhounds, Thoroughbred (All AU), Harness (NSW), **All (AU)**, Greyhounds (NZ), Harness (NZ), Thoroughbred (NZ), **All (AU+NZ)**.
- **Timezone selector**: default `Australia/Sydney`; also `Pacific/Auckland` for NZ.
- **Refresh loaded data**: forces meetings/races to reload (helpful when sources update or caching hides a venue).
- **Race roster (what’s run / what’s next)**:
  - Show finished races (default ON)
  - Only show next upcoming per venue (default OFF)
  - Show best pick (optional; can be slow)
- **WHY** and **Odds**: per-row popovers in interactive roster mode.
- **Daily review (winners vs our picks)**: compares saved picks vs fetched results (best-effort).

## Run (CLI)

### Greyhounds

```bash
python dog_rater_live.py --date 2026-02-06 --venue "Wentworth" --race 1
```

### Horses

Thoroughbred (All AU):

```bash
python horse_rater_live.py --code thoroughbred --date 2026-02-07 --venue "Caulfield" --race 1
```

Harness (NSW):

```bash
python horse_rater_live.py --code harness --date 2026-02-06 --venue "Newcastle" --race 1
```


