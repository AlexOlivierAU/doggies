from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, time
from typing import Any, Optional


Code = str  # "greyhound" | "thoroughbred" | "harness"


@dataclass(frozen=True)
class Meeting:
    code: Code
    source: str
    venue: str
    meeting_date: date
    first_race_time_local: Optional[time]
    num_races: Optional[int]
    meeting_url: str
    status: str  # "upcoming" | "in_progress" | "finished" | "unknown"
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Race:
    code: Code
    race_no: int
    name: str
    distance_m: Optional[int]
    start_time_local: Optional[time]
    race_url: Optional[str]
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Runner:
    code: Code
    name: str
    draw: Optional[int]  # greyhound box / horse barrier / harness gate
    recent_finishes: list[int]  # newest-first or any order; scorer is defensive
    early_speed: Optional[float]  # greyhounds: split time proxy; horses/harness: optional
    age: Optional[int] = None
    sex: Optional[str] = None
    profile_url: Optional[str] = None
    weight_kg: Optional[float] = None
    benchmark: Optional[float] = None
    trainer: Optional[str] = None
    jockey_or_driver: Optional[str] = None
    last10: Optional[str] = None
    scratched: bool = False
    silk_url: Optional[str] = None  # jockey silks image URL (thoroughbred)
    raw: dict[str, Any] = field(default_factory=dict)
    program_number: Optional[int] = None  # official saddle/program No; not barrier

