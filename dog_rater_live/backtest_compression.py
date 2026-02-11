"""
Compression index backtest for thoroughbred (gallop) selections.

Measures whether small score gaps (Rank 1 vs 2, Rank 1 vs 3) correlate with
place-heavy outcomes: top pick places but doesn't win.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

from parse_racingaustralia import (
    fetch_meetings_for_date,
    fetch_races_and_runners_for_meeting,
)
from review import (
    RaceResult,
    fetch_results_for_meeting,
)
from scoring import rank_runners, suggest_auto_weights


def _normalize_name(s: str) -> str:
    """Normalize runner name for matching (lowercase, collapse spaces, strip suffix metadata)."""
    s = re.sub(r"\s+", " ", (s or "").strip().lower())
    for sep in [" nbt", " t:", " r/t:", " trainer:"]:
        if sep in s:
            s = s.split(sep, 1)[0].strip()
    return s


def _name_matches(a: str, b: str) -> bool:
    """True if normalized names match or one contains the other (handles truncation)."""
    na, nb = _normalize_name(a), _normalize_name(b)
    if na == nb:
        return True
    # Allow partial match for "HORSE NAME" vs "HORSE NAME (NZ)" etc
    return na in nb or nb in na


@dataclass
class RaceMetrics:
    """Per-race compression and outcome metrics."""

    meeting_url: str
    venue: str
    meeting_date: date
    race_no: int
    score_rank1: float
    score_rank2: float
    score_rank3: float
    score_diff_1_2: float
    score_diff_1_3: float
    compression_index: float  # score_rank1 - score_rank3
    rank1_name: str
    rank2_name: str
    rank3_name: str
    # Actual placings (1st, 2nd, 3rd)
    actual_1st: Optional[str]
    actual_2nd: Optional[str]
    actual_3rd: Optional[str]
    # Outcomes
    rank1_won: bool
    rank1_placed: bool  # 1st, 2nd, or 3rd
    rank2_placed: bool
    rank3_placed: bool


def run_backtest(
    start_date: date,
    end_date: Optional[date] = None,
    *,
    threshold_percentile: float = 25.0,
    ttl_seconds: int = 300,
) -> tuple[list[RaceMetrics], float, dict]:
    """
    Backtest recent TB races.

    Returns (metrics_list, suggested_threshold, summary_dict).
    threshold_percentile: races with compression_index below this percentile are "clustered".
    """
    if end_date is None:
        end_date = start_date
    all_metrics: list[RaceMetrics] = []
    d = start_date
    while d <= end_date:
        meetings = fetch_meetings_for_date(d, ttl_seconds=ttl_seconds)
        for m in meetings:
            if m.code != "thoroughbred":
                continue
            try:
                races, runners_by_race, meta = fetch_races_and_runners_for_meeting(
                    m.meeting_url, ttl_seconds=ttl_seconds
                )
            except Exception:
                continue
            try:
                results = fetch_results_for_meeting("thoroughbred", m.meeting_url)
            except Exception:
                results = {}
            track_condition = (meta or {}).get("track_condition")
            for race in races:
                runners = runners_by_race.get(race.race_no, [])
                res = results.get(race.race_no)
                if not res or not res.winner or len(runners) < 3:
                    continue
                bw, fw, ew, _ = suggest_auto_weights(runners, track_condition=track_condition)
                ranked = rank_runners(
                    runners,
                    box_weight=bw,
                    form_weight=fw,
                    early_weight=ew,
                    track_condition=track_condition,
                )
                if len(ranked) < 3:
                    continue
                r1, r2, r3 = ranked[0], ranked[1], ranked[2]
                score_diff_1_2 = r1.score - r2.score
                score_diff_1_3 = r1.score - r3.score
                compression_index = score_diff_1_3
                places = getattr(res, "places", ()) or ()
                actual_1st = res.winner
                actual_2nd = places[1] if len(places) >= 2 else None
                actual_3rd = places[2] if len(places) >= 3 else None

                def placed(name: Optional[str]) -> bool:
                    if not name:
                        return False
                    for p in [actual_1st, actual_2nd, actual_3rd]:
                        if p and _name_matches(name, p):
                            return True
                    return False

                rank1_won = actual_1st and _name_matches(r1.name, actual_1st)
                rank1_placed = placed(r1.name)
                rank2_placed = placed(r2.name)
                rank3_placed = placed(r3.name)

                all_metrics.append(
                    RaceMetrics(
                        meeting_url=m.meeting_url,
                        venue=m.venue,
                        meeting_date=d,
                        race_no=race.race_no,
                        score_rank1=r1.score,
                        score_rank2=r2.score,
                        score_rank3=r3.score,
                        score_diff_1_2=score_diff_1_2,
                        score_diff_1_3=score_diff_1_3,
                        compression_index=compression_index,
                        rank1_name=r1.name,
                        rank2_name=r2.name,
                        rank3_name=r3.name,
                        actual_1st=actual_1st,
                        actual_2nd=actual_2nd,
                        actual_3rd=actual_3rd,
                        rank1_won=rank1_won,
                        rank1_placed=rank1_placed,
                        rank2_placed=rank2_placed,
                        rank3_placed=rank3_placed,
                    )
                )
        d += timedelta(days=1)

    # Compute threshold from distribution (percentile)
    if all_metrics:
        sorted_ci = sorted(m.compression_index for m in all_metrics)
        idx = max(0, min(len(sorted_ci) - 1, int(len(sorted_ci) * threshold_percentile / 100)))
        suggested_threshold = sorted_ci[idx]
    else:
        suggested_threshold = 0.0

    clustered = [m for m in all_metrics if m.compression_index < suggested_threshold]
    clear_edge = [m for m in all_metrics if m.compression_index >= suggested_threshold]

    def win_rate(ms: list[RaceMetrics]) -> float:
        if not ms:
            return 0.0
        return sum(1 for m in ms if m.rank1_won) / len(ms)

    def place_rate_r2(ms: list[RaceMetrics]) -> float:
        if not ms:
            return 0.0
        return sum(1 for m in ms if m.rank2_placed) / len(ms)

    def place_rate_r3(ms: list[RaceMetrics]) -> float:
        if not ms:
            return 0.0
        return sum(1 for m in ms if m.rank3_placed) / len(ms)

    def avg_compression(ms: list[RaceMetrics]) -> float:
        if not ms:
            return 0.0
        return sum(m.compression_index for m in ms) / len(ms)

    # "Margin of victory" = compression_index when R1 won vs when R1 didn't win
    margin_when_won = [m.compression_index for m in all_metrics if m.rank1_won]
    margin_when_place_only = [m.compression_index for m in all_metrics if m.rank1_placed and not m.rank1_won]
    margin_when_out = [m.compression_index for m in all_metrics if not m.rank1_placed]

    summary = {
        "total_races": len(all_metrics),
        "threshold": suggested_threshold,
        "threshold_percentile": threshold_percentile,
        "clustered_count": len(clustered),
        "clear_edge_count": len(clear_edge),
        "clustered_win_rate": win_rate(clustered),
        "clear_edge_win_rate": win_rate(clear_edge),
        "clustered_place_rate_r2": place_rate_r2(clustered),
        "clustered_place_rate_r3": place_rate_r3(clustered),
        "clustered_avg_compression": avg_compression(clustered),
        "clear_edge_avg_compression": avg_compression(clear_edge),
        "avg_margin_when_won": sum(margin_when_won) / len(margin_when_won) if margin_when_won else 0.0,
        "avg_margin_when_place_only": sum(margin_when_place_only) / len(margin_when_place_only)
        if margin_when_place_only else 0.0,
        "avg_margin_when_out": sum(margin_when_out) / len(margin_when_out) if margin_when_out else 0.0,
        "overall_win_rate": win_rate(all_metrics),
    }

    return all_metrics, suggested_threshold, summary


def format_report(summary: dict) -> str:
    """Format backtest summary as readable text."""
    lines = [
        "=" * 60,
        "Compression Index Backtest — Summary",
        "=" * 60,
        "",
        f"Total races with results: {summary['total_races']}",
        f"Clustered (compression < threshold): {summary['clustered_count']}",
        f"Clear edge (compression >= threshold): {summary['clear_edge_count']}",
        f"Threshold (P{summary['threshold_percentile']:.0f}): {summary['threshold']:.4f}",
        "",
        "--- Win rate of Rank 1 ---",
        f"  Clustered:  {summary['clustered_win_rate']:.1%}",
        f"  Clear edge: {summary['clear_edge_win_rate']:.1%}",
        f"  Overall:    {summary['overall_win_rate']:.1%}",
        "",
        "--- Place rate of Rank 2 & 3 in clustered races ---",
        f"  Rank 2 placed: {summary['clustered_place_rate_r2']:.1%}",
        f"  Rank 3 placed: {summary['clustered_place_rate_r3']:.1%}",
        "",
        "--- Average compression index by outcome ---",
        f"  When R1 won:       {summary['avg_margin_when_won']:.4f}",
        f"  When R1 placed 2/3: {summary['avg_margin_when_place_only']:.4f}",
        f"  When R1 unplaced:   {summary['avg_margin_when_out']:.4f}",
        "",
        "--- Average compression by label ---",
        f"  Clustered:  {summary['clustered_avg_compression']:.4f}",
        f"  Clear edge: {summary['clear_edge_avg_compression']:.4f}",
        "",
        "=" * 60,
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    from datetime import date

    end = date.today() - timedelta(days=1)  # yesterday
    start = end - timedelta(days=6)  # last 7 days
    metrics, thresh, summary = run_backtest(start, end)
    print(format_report(summary))
