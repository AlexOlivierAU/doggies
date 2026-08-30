"""Shared Qt item roles for desktop tables. Painting and sorting use these, not parsed display text."""

from __future__ import annotations

from PySide6.QtCore import Qt

_BASE = int(Qt.ItemDataRole.UserRole)

# Keep race-key on UserRole so existing callers that used UserRole still work.
RACE_KEY_ROLE = _BASE + 0
SILK_URL_ROLE = _BASE + 1
PROGRAM_NUMBER_ROLE = _BASE + 2
HORSE_NAME_ROLE = _BASE + 3
BARRIER_ROLE = _BASE + 4
ODDS_ROLE = _BASE + 5
FLUCTUATION_ROLE = _BASE + 6
FLUCTUATION_HISTORY_ROLE = _BASE + 7
CLASS_ARROW_ROLE = _BASE + 8
CLASS_LABEL_ROLE = _BASE + 9
PICK_ROLE = _BASE + 10
SCRATCHED_ROLE = _BASE + 11
CONFIDENCE_ROLE = _BASE + 12
RESULT_STATUS_ROLE = _BASE + 13
ROW_TONE_ROLE = _BASE + 14
DETAIL_ROLE = _BASE + 15
SORT_ROLE = _BASE + 16
URGENCY_ROLE = ROW_TONE_ROLE
SOURCE_ROLE = _BASE + 17
FORM_ROLE = _BASE + 18
JOCKEY_ROLE = _BASE + 19
TRAINER_ROLE = _BASE + 20
WEIGHT_ROLE = _BASE + 21
SCORE_ROLE = _BASE + 22
PROFILE_URL_ROLE = _BASE + 23
LAST_CLASS_ROLE = _BASE + 24
WHY_ROLE = _BASE + 25
COMPACT_PICK_ROLE = _BASE + 26
