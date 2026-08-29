"""Consistent runner-name normalisation for result matching.

Matching is exact after normalisation. Substring / containment matches are
intentionally rejected so an uncertain result is never assigned silently.
"""

from __future__ import annotations

import re

from race_db import normalize_horse_name

_MIN_NAME_LEN = 2
_METADATA_SEPS = (" nbt", " t:", " r/t:", " trainer:")


def normalize_runner_name(name: str) -> str:
    """Lowercase, collapse spaces, strip country suffix and trailing metadata."""
    s = normalize_horse_name(name or "")
    for sep in _METADATA_SEPS:
        if sep in s:
            s = s.split(sep, 1)[0].strip()
    s = re.sub(r"^[\d]+\.\s*", "", s)
    return s.strip()


def names_are_matchable(name: str) -> bool:
    return len(normalize_runner_name(name)) >= _MIN_NAME_LEN


def names_match(a: str, b: str) -> bool:
    """True only when both names normalise to the same non-empty string."""
    na = normalize_runner_name(a)
    nb = normalize_runner_name(b)
    if not na or not nb:
        return False
    if len(na) < _MIN_NAME_LEN or len(nb) < _MIN_NAME_LEN:
        return False
    return na == nb
