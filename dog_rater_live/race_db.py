"""
Persistent SQLite storage for daily race data, picks, and results.

- Daily meetings/fields: persist loaded data by date so we can load from DB and refresh one race.
- Picks: store best_pick/backup (and optional full pick payload) for picks-vs-results.
- Results: store fetched winner/place2/place3 so we can match picks to actual results without re-fetching.
- Jockey rides: join fields + results into a ride ledger for strike-rate tracking.
Uses the same DB file as db_cache (roster.db); adds tables daily_meetings, daily_fields, picks, results, jockey_rides.
"""

from __future__ import annotations

import json
import pickle
import re
import sqlite3
import time
from datetime import date
from pathlib import Path
from typing import Any, Optional

from db_cache import _DEFAULT_DB, default_db_path


def _conn(db_path: Path = _DEFAULT_DB) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(db_path), timeout=10.0)
    c.execute(
        "CREATE TABLE IF NOT EXISTS daily_meetings (date TEXT, code TEXT, data BLOB, updated_at REAL, PRIMARY KEY (date, code))"
    )
    c.execute(
        "CREATE TABLE IF NOT EXISTS daily_fields (date TEXT, meeting_url TEXT, data BLOB, updated_at REAL, PRIMARY KEY (date, meeting_url))"
    )
    c.execute(
        """CREATE TABLE IF NOT EXISTS picks (
            date TEXT, meeting_url TEXT, code TEXT, race_no INTEGER,
            venue TEXT, race_label TEXT, best_pick TEXT, backup TEXT,
            pick_data BLOB, saved_at REAL,
            PRIMARY KEY (date, meeting_url, race_no)
        )"""
    )
    c.execute(
        """CREATE TABLE IF NOT EXISTS results (
            date TEXT, meeting_url TEXT, code TEXT, race_no INTEGER,
            winner TEXT, place2 TEXT, place3 TEXT, source_url TEXT, fetched_at REAL,
            PRIMARY KEY (date, meeting_url, race_no)
        )"""
    )
    c.execute(
        """CREATE TABLE IF NOT EXISTS jockey_rides (
            date TEXT, meeting_url TEXT, code TEXT, race_no INTEGER,
            venue TEXT, jockey_key TEXT, jockey_name TEXT, horse TEXT,
            finish INTEGER, field_size INTEGER, was_our_pick INTEGER,
            updated_at REAL,
            PRIMARY KEY (date, meeting_url, race_no, jockey_key, horse)
        )"""
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_jockey_rides_key ON jockey_rides (jockey_key, code, date)"
    )
    migrate_schema(c)
    return c


def migrate_schema(conn: sqlite3.Connection) -> None:
    """Idempotent SQLite migrations. Safe on empty DBs and existing race-day data."""
    _migrate_picks_columns(conn)
    _migrate_results_columns(conn)
    try:
        conn.commit()
    except Exception:
        pass


def _add_columns(conn: sqlite3.Connection, table: str, columns: tuple[tuple[str, str], ...]) -> None:
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    for col, decl in columns:
        if col not in existing:
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
            except Exception:
                pass


def _migrate_picks_columns(conn: sqlite3.Connection) -> None:
    """Add tracking / snapshot columns to picks if missing (safe to re-run)."""
    _add_columns(
        conn,
        "picks",
        (
            ("roughie", "TEXT"),
            ("best_score", "REAL"),
            ("backup_score", "REAL"),
            ("roughie_score", "REAL"),
            ("field_size", "INTEGER"),
            ("status", "TEXT"),
            ("just_place", "TEXT"),
            ("just_place_score", "REAL"),
            ("locked", "INTEGER"),
            ("locked_at", "REAL"),
            ("confidence_label", "TEXT"),
            ("score_gap", "REAL"),
            ("primary_odds", "REAL"),
            ("backup_odds", "REAL"),
            ("original_primary", "TEXT"),
            ("primary_scratched", "INTEGER"),
            ("backup_promoted", "INTEGER"),
            ("scheduled_jump", "TEXT"),
            ("primary_number", "INTEGER"),
            ("backup_number", "INTEGER"),
            ("original_backup", "TEXT"),
            ("backup_scratched", "INTEGER"),
            ("scratching_source", "TEXT"),
            ("scratching_detected_at", "TEXT"),
            ("active_primary", "TEXT"),
            ("active_backup", "TEXT"),
        ),
    )


def _migrate_results_columns(conn: sqlite3.Connection) -> None:
    _add_columns(
        conn,
        "results",
        (
            ("status", "TEXT"),
            ("error_message", "TEXT"),
        ),
    )


_JOCKEY_CLAIM_RE = re.compile(
    r"""
    \s*
    (?:
        \(a[^)]*\)           # (a), (a1.5), (a2/51kg)
      | \ba(?:\d+(?:\.\d+)?)?(?:/\d+\s*kg)?\b  # trailing a / a1.5 / a2/51kg
    )
    \s*
    """,
    re.IGNORECASE | re.VERBOSE,
)
_HORSE_COUNTRY_RE = re.compile(r"\s*\(([A-Z]{2,3}|NZ|GB|IRE|USA|FR|JPN|GER|ITY)\)\s*$", re.IGNORECASE)
_TITLE_RE = re.compile(r"^(?:miss|ms\.?|mrs\.?|mr\.?)\s+", re.IGNORECASE)


def normalize_jockey_name(name: str) -> str:
    """Collapse apprentice claims / titles so the same rider aggregates together."""
    s = (name or "").strip()
    if not s:
        return ""
    # Drop late-alt / emergency rider notes: "Olivia Chambers , (late alt)"
    s = re.split(r",\s*\(", s, maxsplit=1)[0]
    s = _JOCKEY_CLAIM_RE.sub(" ", s)
    s = _TITLE_RE.sub("", s)
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s


def normalize_horse_name(name: str) -> str:
    s = (name or "").strip()
    if not s:
        return ""
    s = _HORSE_COUNTRY_RE.sub("", s)
    return re.sub(r"\s+", " ", s).strip().lower()


def display_jockey_name(name: str) -> str:
    """Pretty label without claim markers."""
    s = (name or "").strip()
    if not s:
        return ""
    s = re.split(r",\s*\(", s, maxsplit=1)[0]
    s = _JOCKEY_CLAIM_RE.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def db_status(db_path: Path = _DEFAULT_DB) -> dict[str, Any]:
    """Counts and path for UI / tracking health."""
    out: dict[str, Any] = {
        "path": str((db_path if db_path is not None else default_db_path()).resolve()),
        "exists": db_path.exists(),
        "picks": 0,
        "results": 0,
        "daily_fields": 0,
        "daily_meetings": 0,
        "jockey_rides": 0,
        "cache": 0,
        "picks_by_date": [],
        "results_by_date": [],
    }
    try:
        conn = _conn(db_path)
        for table in ("picks", "results", "daily_fields", "daily_meetings", "jockey_rides", "cache"):
            try:
                out[table] = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            except Exception:
                out[table] = 0
        out["picks_by_date"] = conn.execute(
            "SELECT date, COUNT(*) FROM picks GROUP BY date ORDER BY date DESC LIMIT 8"
        ).fetchall()
        out["results_by_date"] = conn.execute(
            "SELECT date, COUNT(*) FROM results GROUP BY date ORDER BY date DESC LIMIT 8"
        ).fetchall()
        conn.close()
    except Exception as e:
        out["error"] = str(e)
    return out


# --- Daily meetings (list of Meeting per date + code) ---


def persist_daily_meetings(d: date, code: str, meetings: list, db_path: Path = _DEFAULT_DB) -> None:
    try:
        conn = _conn(db_path)
        conn.execute(
            "INSERT OR REPLACE INTO daily_meetings (date, code, data, updated_at) VALUES (?, ?, ?, ?)",
            (d.isoformat(), code, pickle.dumps(meetings), time.time()),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def load_daily_meetings(d: date, code: str, db_path: Path = _DEFAULT_DB) -> Optional[list]:
    try:
        conn = _conn(db_path)
        row = conn.execute(
            "SELECT data FROM daily_meetings WHERE date = ? AND code = ?",
            (d.isoformat(), code),
        ).fetchone()
        conn.close()
        if row is None:
            return None
        return pickle.loads(row[0])
    except Exception:
        return None


# --- Daily fields (races + runners_by_race + meta per meeting) ---


def persist_daily_fields(
    d: date,
    meeting_url: str,
    data: tuple,
    db_path: Path = _DEFAULT_DB,
) -> None:
    """data = (races, runners_by_race) or (races, runners_by_race, meta)."""
    try:
        conn = _conn(db_path)
        conn.execute(
            "INSERT OR REPLACE INTO daily_fields (date, meeting_url, data, updated_at) VALUES (?, ?, ?, ?)",
            (d.isoformat(), meeting_url, pickle.dumps(data), time.time()),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def _race_no_int(r) -> Optional[int]:
    n = getattr(r, "race_no", None)
    if n is None:
        return None
    try:
        return int(n)
    except (TypeError, ValueError):
        return None


def merge_meeting_fields(old_data: Optional[tuple], new_data: tuple) -> tuple:
    """
    Merge old meeting data with new. Keeps races (and their runners) that exist in old
    but not in new, so we don't lose just-run races when the source drops them.
    Returns (races, runners_by_race, meta) with new_data's meta preferred.
    race_no is normalized to int for comparison so we don't drop races due to type mismatch.
    """
    if not old_data or not (old_data[0] or []):
        return (new_data[0] or [], new_data[1] if len(new_data) > 1 else {}, new_data[2] if len(new_data) > 2 else {})
    old_races = old_data[0] or []
    old_runners = old_data[1] if len(old_data) > 1 and old_data[1] else {}
    old_meta = old_data[2] if len(old_data) > 2 else {}
    new_races = new_data[0] or []
    new_runners = new_data[1] if len(new_data) > 1 and new_data[1] else {}
    new_meta = new_data[2] if len(new_data) > 2 else {}
    new_nos = {_race_no_int(r) for r in new_races}
    kept_races = [r for r in old_races if _race_no_int(r) not in new_nos]
    merged_races = list(new_races) + kept_races
    merged_runners = dict(new_runners) if new_runners else {}
    for r in kept_races:
        rn = _race_no_int(r)
        if rn is not None:
            # old_runners may key by int or other; try both
            val = (old_runners or {}).get(rn) or (old_runners or {}).get(str(rn))
            if val is not None:
                merged_runners[rn] = val
    return (merged_races, merged_runners, new_meta or old_meta)


def load_daily_fields(
    d: date,
    meeting_url: str,
    db_path: Path = _DEFAULT_DB,
) -> Optional[tuple]:
    """Returns (races, runners_by_race) or (races, runners_by_race, meta)."""
    try:
        conn = _conn(db_path)
        row = conn.execute(
            "SELECT data FROM daily_fields WHERE date = ? AND meeting_url = ?",
            (d.isoformat(), meeting_url),
        ).fetchone()
        conn.close()
        if row is None:
            return None
        return pickle.loads(row[0])
    except Exception:
        return None


def update_race_runners_in_db(
    d: date,
    meeting_url: str,
    race_no: int,
    new_runners: list,
    db_path: Path = _DEFAULT_DB,
) -> bool:
    """Update runners for one race in stored daily_fields. Returns True if updated."""
    try:
        data = load_daily_fields(d, meeting_url, db_path)
        if data is None:
            return False
        if len(data) == 2:
            races, runners_by_race = data
            meta = {}
        else:
            races, runners_by_race, meta = data[0], data[1], (data[2] if len(data) > 2 else {})
        runners_by_race = dict(runners_by_race) if runners_by_race else {}
        runners_by_race[int(race_no)] = new_runners
        persist_daily_fields(d, meeting_url, (races, runners_by_race, meta), db_path)
        return True
    except Exception:
        return False


# --- Picks (best_pick, backup, optional full payload) ---


_PICK_SELECT = """
    date, meeting_url, code, race_no, venue, race_label, best_pick, backup,
    pick_data, saved_at, roughie, best_score, backup_score, roughie_score,
    field_size, status, just_place, just_place_score,
    locked, locked_at, confidence_label, score_gap, primary_odds, backup_odds,
    original_primary, primary_scratched, backup_promoted, scheduled_jump,
    primary_number, backup_number,
    original_backup, backup_scratched, scratching_source, scratching_detected_at,
    active_primary, active_backup
"""


def _row_to_pick(r: tuple) -> dict:
    (
        dt,
        mu,
        code,
        rn,
        venue,
        race_label,
        best_pick,
        backup,
        pick_data_blob,
        saved_at,
        roughie,
        best_score,
        backup_score,
        roughie_score,
        field_size,
        status,
        just_place,
        just_place_score,
        locked,
        locked_at,
        confidence_label,
        score_gap,
        primary_odds,
        backup_odds,
        original_primary,
        primary_scratched,
        backup_promoted,
        scheduled_jump,
        primary_number,
        backup_number,
        original_backup,
        backup_scratched,
        scratching_source,
        scratching_detected_at,
        active_primary,
        active_backup,
    ) = r
    extra = {
        "locked": bool(locked),
        "locked_at": locked_at,
        "confidence_label": confidence_label or "",
        "score_gap": score_gap,
        "primary_odds": primary_odds,
        "backup_odds": backup_odds,
        "original_primary": original_primary or "",
        "primary_scratched": bool(primary_scratched),
        "backup_promoted": bool(backup_promoted),
        "scheduled_jump": scheduled_jump or "",
        "primary_number": primary_number,
        "backup_number": backup_number,
        "original_backup": original_backup or "",
        "backup_scratched": bool(backup_scratched),
        "scratching_source": scratching_source or "",
        "scratching_detected_at": scratching_detected_at or "",
        "active_primary": active_primary or "",
        "active_backup": active_backup or "",
        "saved_at": saved_at,
        "roughie": roughie or "",
        "just_place": just_place or "",
        "field_size": field_size,
        "status": status or "",
    }
    if pick_data_blob:
        try:
            obj = json.loads(pick_data_blob.decode("utf-8"))
            obj.setdefault("meeting_url", mu)
            obj.setdefault("race_no", rn)
            obj.setdefault("code", code)
            obj.setdefault("venue", venue)
            obj.setdefault("meeting_date", dt)
            obj.setdefault("pick_name", best_pick)
            obj.setdefault("backup", backup or "")
            obj.setdefault("roughie", roughie or "")
            obj.setdefault("just_place", just_place or "")
            obj.setdefault("race_label", race_label or f"R{rn}")
            if best_score is not None:
                obj.setdefault("pick_score", best_score)
            cond = obj.setdefault("conditions", {})
            if isinstance(cond, dict):
                if backup_score is not None:
                    cond.setdefault("backup_score", backup_score)
                if roughie_score is not None:
                    cond.setdefault("roughie_score", roughie_score)
                if just_place_score is not None:
                    cond.setdefault("just_place_score", just_place_score)
                if field_size is not None:
                    cond.setdefault("field_size", field_size)
                if status:
                    cond.setdefault("status", status)
                if just_place:
                    cond.setdefault("just_place", just_place)
            obj.update(extra)
            if obj.get("primary_number") is None:
                snap = obj.get("snapshot") if isinstance(obj.get("snapshot"), dict) else {}
                obj["primary_number"] = snap.get("primary_number")
            if obj.get("backup_number") is None:
                snap = obj.get("snapshot") if isinstance(obj.get("snapshot"), dict) else {}
                obj["backup_number"] = snap.get("backup_number")
            return obj
        except Exception:
            pass
    return {
        "meeting_date": dt,
        "meeting_url": mu,
        "code": code,
        "venue": venue or "",
        "race_no": rn,
        "race_label": race_label or f"R{rn}",
        "pick_name": best_pick or "",
        "backup": backup or "",
        "roughie": roughie or "",
        "just_place": just_place or "",
        "pick_score": best_score,
        "picked_at_iso": "",
        "key_factors": "",
        "why_bullets": [],
        "history_bullets": [],
        "weights": {},
        "conditions": {
            "backup_score": backup_score,
            "roughie_score": roughie_score,
            "just_place_score": just_place_score,
            "field_size": field_size,
            "status": status or "",
            "just_place": just_place or "",
        },
        **extra,
    }


def get_pick(
    d: date,
    meeting_url: str,
    race_no: int,
    db_path: Path = _DEFAULT_DB,
) -> Optional[dict]:
    try:
        conn = _conn(db_path)
        row = conn.execute(
            f"SELECT {_PICK_SELECT} FROM picks WHERE date = ? AND meeting_url = ? AND race_no = ?",
            (d.isoformat(), meeting_url, int(race_no)),
        ).fetchone()
        conn.close()
        if row is None:
            return None
        return _row_to_pick(row)
    except Exception:
        return None


def save_pick(
    d: date,
    meeting_url: str,
    code: str,
    race_no: int,
    venue: str,
    race_label: str,
    best_pick: str,
    backup: str = "",
    pick_data: Optional[dict] = None,
    *,
    roughie: str = "",
    best_score: Optional[float] = None,
    backup_score: Optional[float] = None,
    roughie_score: Optional[float] = None,
    field_size: Optional[int] = None,
    status: str = "",
    just_place: str = "",
    just_place_score: Optional[float] = None,
    locked: Optional[bool] = None,
    locked_at: Optional[float] = None,
    confidence_label: str = "",
    score_gap: Optional[float] = None,
    primary_odds: Optional[float] = None,
    backup_odds: Optional[float] = None,
    original_primary: str = "",
    primary_scratched: Optional[bool] = None,
    backup_promoted: Optional[bool] = None,
    scheduled_jump: str = "",
    primary_number: Optional[int] = None,
    backup_number: Optional[int] = None,
    original_backup: str = "",
    backup_scratched: Optional[bool] = None,
    scratching_source: str = "",
    scratching_detected_at: str = "",
    active_primary: str = "",
    active_backup: str = "",
    force: bool = False,
    db_path: Path = _DEFAULT_DB,
) -> bool:
    """Upsert a pick for (date, meeting_url, race_no). Returns False if a locked snapshot blocked the write."""
    try:
        conn = _conn(db_path)
        existing = conn.execute(
            f"SELECT {_PICK_SELECT} FROM picks WHERE date = ? AND meeting_url = ? AND race_no = ?",
            (d.isoformat(), meeting_url, int(race_no)),
        ).fetchone()
        existing_pick = _row_to_pick(existing) if existing else None
        if existing_pick and existing_pick.get("locked") and not force:
            conn.close()
            return False

        blob = json.dumps(pick_data, ensure_ascii=False).encode("utf-8") if pick_data else None
        if pick_data:
            cond = pick_data.get("conditions") or {}
            snap = pick_data.get("snapshot") or {}
            if not roughie:
                roughie = str(cond.get("roughie") or pick_data.get("roughie") or "")
            if not just_place:
                just_place = str(cond.get("just_place") or pick_data.get("just_place") or "")
            if best_score is None and pick_data.get("pick_score") is not None:
                try:
                    best_score = float(pick_data.get("pick_score"))
                except (TypeError, ValueError):
                    pass
            if backup_score is None and cond.get("backup_score") is not None:
                try:
                    backup_score = float(cond.get("backup_score"))
                except (TypeError, ValueError):
                    pass
            if roughie_score is None and cond.get("roughie_score") is not None:
                try:
                    roughie_score = float(cond.get("roughie_score"))
                except (TypeError, ValueError):
                    pass
            if just_place_score is None and cond.get("just_place_score") is not None:
                try:
                    just_place_score = float(cond.get("just_place_score"))
                except (TypeError, ValueError):
                    pass
            if field_size is None and cond.get("field_size") is not None:
                try:
                    field_size = int(cond.get("field_size"))
                except (TypeError, ValueError):
                    pass
            if not status:
                status = str(cond.get("status") or pick_data.get("status") or "")
            if not confidence_label:
                confidence_label = str(snap.get("confidence_label") or pick_data.get("confidence_label") or "")
            if score_gap is None and snap.get("score_gap") is not None:
                try:
                    score_gap = float(snap.get("score_gap"))
                except (TypeError, ValueError):
                    pass
            if primary_odds is None and snap.get("primary_odds") is not None:
                try:
                    primary_odds = float(snap.get("primary_odds"))
                except (TypeError, ValueError):
                    pass
            if backup_odds is None and snap.get("backup_odds") is not None:
                try:
                    backup_odds = float(snap.get("backup_odds"))
                except (TypeError, ValueError):
                    pass
            if not scheduled_jump:
                scheduled_jump = str(snap.get("scheduled_jump") or pick_data.get("scheduled_jump") or "")
            if not original_primary:
                original_primary = str(snap.get("original_primary") or pick_data.get("original_primary") or "")
            if primary_number is None:
                raw_no = snap.get("primary_number")
                if raw_no is None:
                    raw_no = pick_data.get("primary_number")
                try:
                    primary_number = int(raw_no) if raw_no not in (None, "") else None
                except (TypeError, ValueError):
                    primary_number = None
            if backup_number is None:
                raw_no = snap.get("backup_number")
                if raw_no is None:
                    raw_no = pick_data.get("backup_number")
                try:
                    backup_number = int(raw_no) if raw_no not in (None, "") else None
                except (TypeError, ValueError):
                    backup_number = None

        if existing_pick:
            if locked is None:
                locked = bool(existing_pick.get("locked"))
            if locked_at is None:
                locked_at = existing_pick.get("locked_at")
            if not original_primary:
                original_primary = existing_pick.get("original_primary") or existing_pick.get("pick_name") or ""
            if primary_scratched is None:
                primary_scratched = bool(existing_pick.get("primary_scratched"))
            if backup_promoted is None:
                backup_promoted = bool(existing_pick.get("backup_promoted"))
            if primary_number is None:
                primary_number = existing_pick.get("primary_number")
            if backup_number is None:
                backup_number = existing_pick.get("backup_number")
            if not original_backup:
                original_backup = existing_pick.get("original_backup") or existing_pick.get("backup") or ""
            if backup_scratched is None:
                backup_scratched = bool(existing_pick.get("backup_scratched"))
            if not scratching_source:
                scratching_source = existing_pick.get("scratching_source") or ""
            if not scratching_detected_at:
                scratching_detected_at = existing_pick.get("scratching_detected_at") or ""
            if not active_primary:
                active_primary = existing_pick.get("active_primary") or ""
            if not active_backup:
                active_backup = existing_pick.get("active_backup") or ""
            if not blob and existing[8]:
                blob = existing[8]
            if primary_odds is None:
                primary_odds = existing_pick.get("primary_odds")
            if backup_odds is None:
                backup_odds = existing_pick.get("backup_odds")
            if score_gap is None:
                score_gap = existing_pick.get("score_gap")
            if not confidence_label:
                confidence_label = existing_pick.get("confidence_label") or ""
            if not scheduled_jump:
                scheduled_jump = existing_pick.get("scheduled_jump") or ""

        if not original_primary:
            original_primary = best_pick or ""
        if not original_backup:
            original_backup = backup or ""

        conn.execute(
            """INSERT OR REPLACE INTO picks
               (date, meeting_url, code, race_no, venue, race_label, best_pick, backup,
                pick_data, saved_at, roughie, best_score, backup_score, roughie_score,
                field_size, status, just_place, just_place_score,
                locked, locked_at, confidence_label, score_gap, primary_odds, backup_odds,
                original_primary, primary_scratched, backup_promoted, scheduled_jump,
                primary_number, backup_number,
                original_backup, backup_scratched, scratching_source, scratching_detected_at,
                active_primary, active_backup)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                d.isoformat(),
                meeting_url,
                code,
                race_no,
                venue,
                race_label,
                best_pick or "",
                backup or "",
                blob,
                time.time(),
                roughie or "",
                best_score,
                backup_score,
                roughie_score,
                field_size,
                status or "",
                just_place or "",
                just_place_score,
                1 if locked else 0,
                locked_at,
                confidence_label or "",
                score_gap,
                primary_odds,
                backup_odds,
                original_primary or "",
                1 if primary_scratched else 0,
                1 if backup_promoted else 0,
                scheduled_jump or "",
                primary_number,
                backup_number,
                original_backup or "",
                1 if backup_scratched else 0,
                scratching_source or "",
                scratching_detected_at or "",
                active_primary or "",
                active_backup or "",
            ),
        )
        conn.commit()
        conn.close()
        return True
    except Exception:
        return False


def lock_pick(
    d: date,
    meeting_url: str,
    race_no: int,
    db_path: Path = _DEFAULT_DB,
) -> bool:
    try:
        conn = _conn(db_path)
        conn.execute(
            """UPDATE picks SET locked = 1, locked_at = COALESCE(locked_at, ?)
               WHERE date = ? AND meeting_url = ? AND race_no = ?""",
            (time.time(), d.isoformat(), meeting_url, int(race_no)),
        )
        conn.commit()
        changed = conn.total_changes > 0
        conn.close()
        return changed
    except Exception:
        return False


def mark_primary_scratched(
    d: date,
    meeting_url: str,
    race_no: int,
    db_path: Path = _DEFAULT_DB,
    *,
    source: str = "",
    detected_at: str = "",
    active_primary: str = "",
    active_backup: str = "",
    backup_scratched: Optional[bool] = None,
    backup_promoted: Optional[bool] = None,
    original_backup: str = "",
) -> bool:
    """Preserve original primary and record late-scratching / promotion flags. Idempotent."""
    try:
        conn = _conn(db_path)
        row = conn.execute(
            """SELECT best_pick, original_primary, backup, original_backup, primary_scratched,
                      backup_promoted, backup_scratched, scratching_source, scratching_detected_at,
                      active_primary, active_backup
               FROM picks WHERE date = ? AND meeting_url = ? AND race_no = ?""",
            (d.isoformat(), meeting_url, int(race_no)),
        ).fetchone()
        if row is None:
            conn.close()
            return False
        (
            best_pick,
            original_primary,
            backup,
            orig_backup,
            already_primary,
            already_promoted,
            already_backup,
            existing_source,
            existing_at,
            existing_active_p,
            existing_active_b,
        ) = row
        preserved = (original_primary or "").strip() or (best_pick or "")
        preserved_backup = (orig_backup or "").strip() or (original_backup or backup or "")
        sources = [s for s in str(existing_source or "").split(",") if s]
        if source and source not in sources:
            sources.append(source)
        new_active_p = active_primary or existing_active_p or ""
        new_active_b = active_backup if active_backup else (existing_active_b or "")
        detected = detected_at or existing_at or ""
        bscratch = already_backup if backup_scratched is None else (1 if backup_scratched else 0)
        if backup_promoted is None:
            promote = bool(active_primary or backup) and not bool(bscratch)
            promoted = 1 if (already_promoted or promote) else 0
        else:
            promoted = 1 if backup_promoted else 0
        # No-op if flags and actives already match.
        if (
            already_primary
            and bool(already_promoted) == bool(promoted)
            and (existing_source or "") == ",".join(sources)
            and (existing_active_p or "") == (new_active_p or "")
            and (existing_active_b or "") == (new_active_b or "")
            and bool(already_backup) == bool(bscratch)
        ):
            conn.close()
            return True
        conn.execute(
            """UPDATE picks
               SET primary_scratched = 1,
                   backup_promoted = ?,
                   original_primary = ?,
                   original_backup = COALESCE(NULLIF(original_backup, ''), ?),
                   backup_scratched = ?,
                   scratching_source = ?,
                   scratching_detected_at = COALESCE(NULLIF(scratching_detected_at, ''), ?),
                   active_primary = ?,
                   active_backup = ?
               WHERE date = ? AND meeting_url = ? AND race_no = ?""",
            (
                promoted,
                preserved,
                preserved_backup,
                1 if bscratch else 0,
                ",".join(sources),
                detected,
                new_active_p or "",
                new_active_b or "",
                d.isoformat(),
                meeting_url,
                int(race_no),
            ),
        )
        conn.commit()
        conn.close()
        return True
    except Exception:
        return False


def load_picks(
    d: date,
    meeting_url: Optional[str] = None,
    db_path: Path = _DEFAULT_DB,
) -> list[dict]:
    """Load picks for date (optionally for one meeting). Returns list of dicts compatible with Daily review (pick_name, meeting_url, race_no, venue, code, etc.)."""
    try:
        conn = _conn(db_path)
        if meeting_url:
            rows = conn.execute(
                f"SELECT {_PICK_SELECT} FROM picks WHERE date = ? AND meeting_url = ? ORDER BY race_no",
                (d.isoformat(), meeting_url),
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT {_PICK_SELECT} FROM picks WHERE date = ? ORDER BY meeting_url, race_no",
                (d.isoformat(),),
            ).fetchall()
        conn.close()
        return [_row_to_pick(r) for r in rows]
    except Exception:
        return []


def load_picks_range(
    date_from: date,
    date_to: date,
    db_path: Path = _DEFAULT_DB,
) -> list[dict]:
    """Load picks inclusive of date_from..date_to."""
    try:
        conn = _conn(db_path)
        rows = conn.execute(
            f"""SELECT {_PICK_SELECT} FROM picks
                WHERE date >= ? AND date <= ?
                ORDER BY date, meeting_url, race_no""",
            (date_from.isoformat(), date_to.isoformat()),
        ).fetchall()
        conn.close()
        return [_row_to_pick(r) for r in rows]
    except Exception:
        return []


# --- Results (winner, place2, place3 per race) ---


def persist_results(
    d: date,
    meeting_url: str,
    code: str,
    results_by_race: dict,
    db_path: Path = _DEFAULT_DB,
) -> None:
    """results_by_race: dict[int, RaceResult] or dict[int, dict with winner, place2, place3, source_url."""
    try:
        conn = _conn(db_path)
        for race_no, res in (results_by_race or {}).items():
            winner = getattr(res, "winner", None) or (res.get("winner") if isinstance(res, dict) else None)
            places = getattr(res, "places", None)
            if places is None and isinstance(res, dict):
                places = res.get("places")
            if not places:
                places = ()
            place2 = places[1] if len(places) > 1 else (res.get("place2") if isinstance(res, dict) else None)
            place3 = places[2] if len(places) > 2 else (res.get("place3") if isinstance(res, dict) else None)
            src = getattr(res, "source_url", None) or (res.get("source_url") if isinstance(res, dict) else None)
            row_status = ""
            err = ""
            if isinstance(res, dict):
                row_status = str(res.get("status") or "")
                err = str(res.get("error_message") or res.get("error") or "")
            if not row_status:
                row_status = "ok" if winner else "empty"
            conn.execute(
                """INSERT OR REPLACE INTO results
                   (date, meeting_url, code, race_no, winner, place2, place3, source_url, fetched_at, status, error_message)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    d.isoformat(),
                    meeting_url,
                    code,
                    race_no,
                    winner or "",
                    place2 or "",
                    place3 or "",
                    src or "",
                    time.time(),
                    row_status,
                    err,
                ),
            )
        conn.commit()
        conn.close()
    except Exception:
        pass
    # Best-effort: join fields + results into jockey ride ledger.
    try:
        sync_jockey_rides_for_meeting(d, meeting_url, code, db_path=db_path)
    except Exception:
        pass


def persist_result_failure(
    d: date,
    meeting_url: str,
    code: str,
    race_no: int,
    error_message: str,
    db_path: Path = _DEFAULT_DB,
) -> None:
    try:
        conn = _conn(db_path)
        existing = conn.execute(
            "SELECT winner, place2, place3, source_url FROM results WHERE date = ? AND meeting_url = ? AND race_no = ?",
            (d.isoformat(), meeting_url, int(race_no)),
        ).fetchone()
        winner = existing[0] if existing else ""
        place2 = existing[1] if existing else ""
        place3 = existing[2] if existing else ""
        src = existing[3] if existing else ""
        conn.execute(
            """INSERT OR REPLACE INTO results
               (date, meeting_url, code, race_no, winner, place2, place3, source_url, fetched_at, status, error_message)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                d.isoformat(),
                meeting_url,
                code,
                int(race_no),
                winner or "",
                place2 or "",
                place3 or "",
                src or "",
                time.time(),
                "error",
                error_message or "result fetch failed",
            ),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass
    # Best-effort: join fields + results into jockey ride ledger.
    try:
        sync_jockey_rides_for_meeting(d, meeting_url, code, db_path=db_path)
    except Exception:
        pass


def load_results(
    d: date,
    meeting_url: str,
    code: str,
    db_path: Path = _DEFAULT_DB,
) -> dict[int, dict]:
    """Returns {race_no: {"winner": str, "place2": str, "place3": str}} for use in Daily review."""
    try:
        conn = _conn(db_path)
        rows = conn.execute(
            "SELECT race_no, winner, place2, place3, status, error_message, source_url FROM results WHERE date = ? AND meeting_url = ? AND code = ?",
            (d.isoformat(), meeting_url, code),
        ).fetchall()
        conn.close()
        return {
            r[0]: {
                "winner": r[1] or "",
                "place2": r[2] or "",
                "place3": r[3] or "",
                "status": r[4] or "",
                "error_message": r[5] or "",
                "source_url": r[6] or "",
            }
            for r in rows
        }
    except Exception:
        return {}


def load_results_for_date(d: date, db_path: Path = _DEFAULT_DB) -> dict[tuple[str, int], dict]:
    """All stored results for a date, keyed by (meeting_url, race_no)."""
    try:
        conn = _conn(db_path)
        rows = conn.execute(
            """SELECT meeting_url, race_no, winner, place2, place3, status, error_message, source_url, code
               FROM results WHERE date = ?""",
            (d.isoformat(),),
        ).fetchall()
        conn.close()
        return {
            (r[0], int(r[1])): {
                "winner": r[2] or "",
                "place2": r[3] or "",
                "place3": r[4] or "",
                "status": r[5] or "",
                "error_message": r[6] or "",
                "source_url": r[7] or "",
                "code": r[8] or "",
            }
            for r in rows
        }
    except Exception:
        return {}


def load_results_range(date_from: date, date_to: date, db_path: Path = _DEFAULT_DB) -> dict[tuple[str, str, int], dict]:
    """Results keyed by (date, meeting_url, race_no)."""
    try:
        conn = _conn(db_path)
        rows = conn.execute(
            """SELECT date, meeting_url, race_no, winner, place2, place3, status, error_message, source_url, code
               FROM results WHERE date >= ? AND date <= ?""",
            (date_from.isoformat(), date_to.isoformat()),
        ).fetchall()
        conn.close()
        return {
            (r[0], r[1], int(r[2])): {
                "winner": r[3] or "",
                "place2": r[4] or "",
                "place3": r[5] or "",
                "status": r[6] or "",
                "error_message": r[7] or "",
                "source_url": r[8] or "",
                "code": r[9] or "",
            }
            for r in rows
        }
    except Exception:
        return {}


# --- Jockey / driver ride ledger ---


def _finish_for_horse(horse: str, winner: str, place2: str, place3: str) -> Optional[int]:
    h = normalize_horse_name(horse)
    if not h:
        return None
    if winner and normalize_horse_name(winner) == h:
        return 1
    if place2 and normalize_horse_name(place2) == h:
        return 2
    if place3 and normalize_horse_name(place3) == h:
        return 3
    return None


def sync_jockey_rides_for_meeting(
    d: date,
    meeting_url: str,
    code: str,
    *,
    venue: str = "",
    db_path: Path = _DEFAULT_DB,
) -> int:
    """
    Rebuild jockey_rides rows for this meeting from daily_fields + results (+ picks).
    Returns number of ride rows written. No-op when fields or results are missing.
    """
    fields = load_daily_fields(d, meeting_url, db_path=db_path)
    if not fields:
        return 0
    runners_by_race = fields[1] if len(fields) > 1 else {}
    if not runners_by_race:
        return 0

    results = load_results(d, meeting_url, code, db_path=db_path)
    if not results:
        return 0

    # Optional: our best pick per race for "rode our tip" stats.
    best_by_race: dict[int, str] = {}
    try:
        conn = _conn(db_path)
        for row in conn.execute(
            "SELECT race_no, best_pick, venue FROM picks WHERE date = ? AND meeting_url = ?",
            (d.isoformat(), meeting_url),
        ):
            rn, bp, ven = row
            try:
                best_by_race[int(rn)] = bp or ""
            except Exception:
                pass
            if not venue and ven:
                venue = ven or ""
        conn.close()
    except Exception:
        pass

    now = time.time()
    rows_out: list[tuple] = []
    for race_no, res in results.items():
        try:
            rn = int(race_no)
        except Exception:
            continue
        runners = runners_by_race.get(rn) or runners_by_race.get(str(rn)) or []
        if not runners:
            continue
        winner = (res.get("winner") if isinstance(res, dict) else "") or ""
        place2 = (res.get("place2") if isinstance(res, dict) else "") or ""
        place3 = (res.get("place3") if isinstance(res, dict) else "") or ""
        if not winner and not place2 and not place3:
            continue

        active = [r for r in runners if not bool(getattr(r, "scratched", False))]
        field_size = len(active) if active else len(runners)
        our_pick = normalize_horse_name(best_by_race.get(rn) or "")

        for r in active or runners:
            jockey_raw = str(getattr(r, "jockey_or_driver", None) or "").strip()
            jockey_key = normalize_jockey_name(jockey_raw)
            if not jockey_key:
                continue
            horse = str(getattr(r, "name", "") or "").strip()
            if not horse:
                continue
            finish = _finish_for_horse(horse, winner, place2, place3)
            # Unplaced but started: store 0 so rides count; only placings get 1/2/3.
            finish_val = finish if finish is not None else 0
            was_pick = 1 if our_pick and normalize_horse_name(horse) == our_pick else 0
            rows_out.append(
                (
                    d.isoformat(),
                    meeting_url,
                    code,
                    rn,
                    venue or "",
                    jockey_key,
                    display_jockey_name(jockey_raw) or jockey_raw,
                    horse,
                    finish_val,
                    field_size,
                    was_pick,
                    now,
                )
            )

    if not rows_out:
        return 0

    try:
        conn = _conn(db_path)
        conn.execute(
            "DELETE FROM jockey_rides WHERE date = ? AND meeting_url = ? AND code = ?",
            (d.isoformat(), meeting_url, code),
        )
        conn.executemany(
            """INSERT OR REPLACE INTO jockey_rides
               (date, meeting_url, code, race_no, venue, jockey_key, jockey_name, horse,
                finish, field_size, was_our_pick, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows_out,
        )
        conn.commit()
        conn.close()
        return len(rows_out)
    except Exception:
        return 0


def backfill_jockey_rides(db_path: Path = _DEFAULT_DB) -> dict[str, int]:
    """Rebuild jockey_rides from all stored results that have matching daily_fields."""
    written = 0
    meetings = 0
    try:
        conn = _conn(db_path)
        rows = conn.execute(
            "SELECT DISTINCT date, meeting_url, code FROM results ORDER BY date"
        ).fetchall()
        conn.close()
    except Exception:
        return {"meetings": 0, "rides": 0}
    for date_s, meeting_url, code in rows:
        try:
            d = date.fromisoformat(date_s)
        except Exception:
            continue
        n = sync_jockey_rides_for_meeting(d, meeting_url, code or "thoroughbred", db_path=db_path)
        meetings += 1
        written += n
    return {"meetings": meetings, "rides": written}


def jockey_stats(
    *,
    code: str = "thoroughbred",
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    min_rides: int = 3,
    limit: int = 40,
    db_path: Path = _DEFAULT_DB,
) -> list[dict[str, Any]]:
    """
    Aggregate ride ledger into a leaderboard.
    Sorted by place%, then wins, then rides (min_rides floor).
    """
    where = ["1=1"]
    params: list[Any] = []
    if code:
        where.append("code = ?")
        params.append(code)
    if date_from is not None:
        where.append("date >= ?")
        params.append(date_from.isoformat())
    if date_to is not None:
        where.append("date <= ?")
        params.append(date_to.isoformat())
    sql = f"""
        SELECT
            jockey_key,
            MAX(jockey_name) AS jockey,
            COUNT(*) AS rides,
            SUM(CASE WHEN finish = 1 THEN 1 ELSE 0 END) AS wins,
            SUM(CASE WHEN finish IN (1, 2, 3) THEN 1 ELSE 0 END) AS places,
            SUM(CASE WHEN was_our_pick = 1 THEN 1 ELSE 0 END) AS pick_rides,
            SUM(CASE WHEN was_our_pick = 1 AND finish = 1 THEN 1 ELSE 0 END) AS pick_wins,
            SUM(CASE WHEN was_our_pick = 1 AND finish IN (1, 2, 3) THEN 1 ELSE 0 END) AS pick_places
        FROM jockey_rides
        WHERE {' AND '.join(where)}
        GROUP BY jockey_key
        HAVING COUNT(*) >= ?
        ORDER BY
            (1.0 * SUM(CASE WHEN finish IN (1, 2, 3) THEN 1 ELSE 0 END) / COUNT(*)) DESC,
            SUM(CASE WHEN finish = 1 THEN 1 ELSE 0 END) DESC,
            COUNT(*) DESC
        LIMIT ?
    """
    params.extend([int(min_rides), int(limit)])
    try:
        conn = _conn(db_path)
        rows = conn.execute(sql, params).fetchall()
        conn.close()
    except Exception:
        return []

    out: list[dict[str, Any]] = []
    for r in rows:
        rides = int(r[2] or 0)
        wins = int(r[3] or 0)
        places = int(r[4] or 0)
        pick_rides = int(r[5] or 0)
        pick_wins = int(r[6] or 0)
        pick_places = int(r[7] or 0)
        out.append(
            {
                "jockey": r[1] or r[0],
                "rides": rides,
                "wins": wins,
                "places": places,
                "win_%": round(100.0 * wins / rides, 1) if rides else 0.0,
                "place_%": round(100.0 * places / rides, 1) if rides else 0.0,
                "pick_rides": pick_rides,
                "pick_wins": pick_wins,
                "pick_places": pick_places,
                "pick_win_%": round(100.0 * pick_wins / pick_rides, 1) if pick_rides else None,
            }
        )
    return out