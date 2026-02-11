from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

from bs4 import BeautifulSoup

from fetch import get
from models import Runner


def greyhound_history_bullets(r: Runner) -> list[str]:
    bullets: list[str] = []
    cols = (r.raw or {}).get("cols") or {}

    if r.recent_finishes:
        bullets.append("Recent finishes: " + "-".join(str(x) for x in r.recent_finishes[:5]))

    if r.early_speed is not None:
        bullets.append(f"Early speed proxy (split-ish): {r.early_speed:.2f}s")

    # best-effort: show a couple of useful card columns if present
    for k in ["TRK/DIST", "LAST START", "Av 1 SEC", "BEST TIME", "LAST 4"]:
        if k in cols and cols[k]:
            bullets.append(f"{k}: {cols[k]}")

    return bullets


def harness_history_bullets(r: Runner) -> list[str]:
    bullets: list[str] = []
    raw = r.raw or {}
    if raw.get("career"):
        bullets.append(f"Career: {raw['career']}")
    if raw.get("bmr"):
        bullets.append(f"BMR: {raw['bmr']}")
    if raw.get("lts"):
        bullets.append(f"LTS: ${raw['lts']}")
    if r.recent_finishes:
        bullets.append("Recent finishes: " + "-".join(str(x) for x in r.recent_finishes[:6]))
    if r.jockey_or_driver:
        bullets.append(f"Driver: {r.jockey_or_driver}")
    if r.trainer:
        bullets.append(f"Trainer: {r.trainer}")
    return bullets


def _parse_summary_line(text: str) -> dict[str, str]:
    # e.g. "Summary: 14-3:2:0 Prizemoney: $70,385 ... "
    out: dict[str, str] = {}
    m = re.search(r"Summary:\s*([0-9]+-[0-9:]+)\b", text, re.IGNORECASE)
    if m:
        out["summary_record"] = m.group(1)
    m = re.search(r"Prizemoney:\s*\\$([0-9,]+)", text, re.IGNORECASE)
    if m:
        out["prizemoney"] = m.group(1)
    m = re.search(r"Prizemoney incl\\. Bonus:\s*\\$([0-9,]+)", text, re.IGNORECASE)
    if m:
        out["prizemoney_incl_bonus"] = m.group(1)
    return out


def racingnsw_horse_history_bullets(profile_url: str) -> list[str]:
    """
    Fetch Racing NSW InteractiveForm HorseFullForm and return a small set of bullets.
    Cached by fetch.py disk cache; callers should also wrap in st.cache_data.
    """
    html = get(profile_url, ttl_seconds=24 * 3600, timeout_seconds=30).text
    soup = BeautifulSoup(html, "html.parser")

    bullets: list[str] = []

    tables = soup.find_all("table")
    if len(tables) >= 2:
        # table[1] contains "Career" header but rows are vertical th/td pairs
        t = tables[1]
        # find the row where th == "Career" and capture the td (summary line)
        for tr in t.find_all("tr"):
            th = tr.find("th")
            td = tr.find("td")
            if not th or not td:
                continue
            key = th.get_text(" ", strip=True)
            val = td.get_text(" ", strip=True)
            if key == "Trainer" and val:
                bullets.append(f"Trainer: {val}")
            if key == "Career" and val:
                info = _parse_summary_line(val)
                if info.get("summary_record"):
                    bullets.append(f"Career summary: {info['summary_record']}")
                if info.get("prizemoney"):
                    bullets.append(f"Prizemoney: ${info['prizemoney']}")
                if info.get("prizemoney_incl_bonus"):
                    bullets.append(f"Prizemoney incl. bonus: ${info['prizemoney_incl_bonus']}")

    # table[2] tends to contain recent runs as rows like "7th of 9" + details
    if len(tables) >= 3:
        runs = []
        for tr in tables[2].find_all("tr"):
            tds = tr.find_all("td")
            if len(tds) < 2:
                continue
            placing = tds[0].get_text(" ", strip=True)
            detail = tds[1].get_text(" ", strip=True)
            if not placing or not detail:
                continue
            if placing.lower().startswith("spell"):
                continue
            runs.append((placing, detail))

        # keep last 3
        for placing, detail in runs[-3:]:
            # compress detail: venue/date/dist/track + any in-running markers if present
            short = detail
            short = re.sub(r"\\s+", " ", short).strip()
            bullets.append(f"Recent run: {placing} — {short[:140]}{'…' if len(short) > 140 else ''}")

        # crude recency: extract "week/s since last race start" row if present
        for placing, detail in runs[::-1]:
            pass

    # Always include a link hint for transparency
    bullets.append(f"Source form: {profile_url}")
    return bullets


def history_bullets_for_runner(r: Runner) -> list[str]:
    if r.code == "greyhound":
        return greyhound_history_bullets(r)
    if r.code == "harness":
        return harness_history_bullets(r)
    if r.code == "thoroughbred":
        if r.profile_url:
            return racingnsw_horse_history_bullets(r.profile_url)
        return []
    return []

