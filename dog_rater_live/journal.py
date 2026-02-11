from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional


JOURNAL_DIR = Path("./journal")


@dataclass(frozen=True)
class PickEntry:
    picked_at_iso: str
    meeting_date: str  # YYYY-MM-DD
    code: str
    venue: str
    meeting_url: str
    race_no: int
    race_name: str
    race_url: str
    pick_name: str
    pick_draw: Optional[int]
    pick_score: float
    key_factors: str
    why_bullets: list[str]
    history_bullets: list[str]
    weights: dict[str, Any]
    conditions: dict[str, Any]


def _path_for_date(d: date) -> Path:
    JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    return JOURNAL_DIR / f"picks_{d.isoformat()}.json"


def load_picks(d: date) -> list[dict[str, Any]]:
    path = _path_for_date(d)
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []


def upsert_pick(entry: PickEntry) -> None:
    """
    Upsert a pick keyed by (code, meeting_url, race_no).
    """
    d = date.fromisoformat(entry.meeting_date)
    path = _path_for_date(d)
    items = load_picks(d)
    key = f"{entry.code}|{entry.meeting_url}|{entry.race_no}"

    out = []
    replaced = False
    for it in items:
        if it.get("_key") == key:
            new_it = {**asdict(entry), "_key": key}
            out.append(new_it)
            replaced = True
        else:
            out.append(it)
    if not replaced:
        out.append({**asdict(entry), "_key": key})

    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")


def make_pick_entry(
    *,
    meeting_date: date,
    code: str,
    venue: str,
    meeting_url: str,
    race_no: int,
    race_name: str,
    race_url: str,
    pick_name: str,
    pick_draw: Optional[int],
    pick_score: float,
    key_factors: str,
    why_bullets: list[str],
    history_bullets: list[str],
    weights: dict[str, Any],
    conditions: dict[str, Any],
) -> PickEntry:
    return PickEntry(
        picked_at_iso=datetime.now().astimezone().isoformat(timespec="seconds"),
        meeting_date=meeting_date.isoformat(),
        code=code,
        venue=venue,
        meeting_url=meeting_url,
        race_no=race_no,
        race_name=race_name,
        race_url=race_url,
        pick_name=pick_name,
        pick_draw=pick_draw,
        pick_score=float(pick_score),
        key_factors=key_factors,
        why_bullets=list(why_bullets),
        history_bullets=list(history_bullets),
        weights=weights,
        conditions=conditions,
    )

