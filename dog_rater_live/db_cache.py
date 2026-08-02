"""
SQLite-backed cache for parsed roster data so the app doesn't re-fetch/re-parse on every start.

Cache keys and TTLs are defined per cache type. Values are pickled (Meeting, Race, Runner, etc.
are dataclasses and pickle-safe). On load we return None if entry is missing or expired.
"""

from __future__ import annotations

import pickle
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

# Default DB next to ./cache/ (same parent as fetch disk cache)
_DEFAULT_DB = Path(__file__).resolve().parent / "cache" / "roster.db"

# TTL in seconds per cache kind (meetings vs fields)
TTL_MEETINGS = 30 * 60   # 30 min
TTL_FIELDS = 15 * 60     # 15 min
TTL_SKY = 30 * 60        # 30 min


def _conn(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(db_path), timeout=10.0)
    c.execute(
        "CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, value BLOB, fetched_at REAL)"
    )
    return c


def get(key: str, ttl_seconds: int, db_path: Path = _DEFAULT_DB) -> Optional[Any]:
    """
    Return cached value if present and not older than ttl_seconds. Otherwise None.
    """
    try:
        conn = _conn(db_path)
        row = conn.execute(
            "SELECT value, fetched_at FROM cache WHERE key = ?", (key,)
        ).fetchone()
        conn.close()
        if row is None:
            return None
        value_blob, fetched_at = row
        if (time.time() - fetched_at) > ttl_seconds:
            return None
        return pickle.loads(value_blob)
    except Exception:
        return None


def set(key: str, value: Any, db_path: Path = _DEFAULT_DB) -> None:
    """Store value (must be pickleable) with current timestamp."""
    try:
        conn = _conn(db_path)
        conn.execute(
            "INSERT OR REPLACE INTO cache (key, value, fetched_at) VALUES (?, ?, ?)",
            (key, pickle.dumps(value), time.time()),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass
