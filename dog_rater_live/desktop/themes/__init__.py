"""Theme identifiers. No Qt imports — safe for settings and tests."""

from __future__ import annotations

THEME_BLUE = "blue"
THEME_DARK = "dark"

THEME_CHOICES = (
    (THEME_BLUE, "Blue"),
    (THEME_DARK, "Classic Dark"),
)
THEME_IDS = tuple(ident for ident, _label in THEME_CHOICES)
THEME_LABELS = {ident: label for ident, label in THEME_CHOICES}
# Previous generic default was stored as "dark" without theme_explicit.
# Classic Dark uses the same id only when the user has marked it explicit.
LEGACY_DEFAULTS = frozenset({"", "dark", "default"})


def normalize_theme_id(value: str | None) -> str:
    raw = str(value or "").strip().lower()
    if raw in {"blue", "navy"}:
        return THEME_BLUE
    if raw in {"dark", "classic dark", "classic", "charcoal"}:
        return THEME_DARK
    return THEME_BLUE


def label_for(ident: str) -> str:
    return THEME_LABELS.get(ident, THEME_LABELS[THEME_BLUE])
