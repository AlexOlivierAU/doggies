from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from models import Runner
from weather import WeatherSnapshot


def clamp01(x: float) -> float:
    if x < 0:
        return 0.0
    if x > 1:
        return 1.0
    return x


def normalize_weights(box_w: float, form_w: float, early_w: float) -> tuple[float, float, float]:
    box_w = max(0.0, float(box_w))
    form_w = max(0.0, float(form_w))
    early_w = max(0.0, float(early_w))
    s = box_w + form_w + early_w
    if s <= 0:
        return (1 / 3, 1 / 3, 1 / 3)
    return (box_w / s, form_w / s, early_w / s)


def _weather_severity(snapshot: Optional[WeatherSnapshot], track_condition: Optional[str] = None) -> float:
    """
    0..1 severity index for 'messy conditions'.
    Used to shift draw bias stronger and reduce reliance on early-speed-like proxies.
    """
    s = 0.0
    if snapshot is not None:
        try:
            precip = float(snapshot.precipitation_mm or 0.0)
            # precipitation is mm (hourly in forecast). >=2mm/h is meaningful.
            s = max(s, clamp01(precip / 2.0))
        except Exception:
            pass
        try:
            wind = float(snapshot.wind_speed_kmh or 0.0)
            s = max(s, clamp01((wind - 15.0) / 25.0))
        except Exception:
            pass
        try:
            hum = float(snapshot.relative_humidity_pct or 0.0)
            s = max(s, clamp01((hum - 70.0) / 25.0))
        except Exception:
            pass
    if track_condition:
        tc = track_condition.strip().lower()
        if tc.startswith("heavy"):
            s = max(s, 0.9)
        elif tc.startswith("soft"):
            s = max(s, 0.65)
        elif tc.startswith("good"):
            s = max(s, s)
    return clamp01(s)


def suggest_auto_weights(
    runners: list[Runner],
    *,
    weather: Optional[WeatherSnapshot] = None,
    track_condition: Optional[str] = None,
) -> tuple[float, float, float, list[str]]:
    """
    "AI-ish" auto weighting (no ML training): choose weights per race based on
    data availability (coverage) and how much the signals vary within the field.

    Returns (box_w, form_w, early_w, rationale_lines).
    """
    if not runners:
        return (1 / 3, 1 / 3, 1 / 3, ["No runners available; using equal weights."])

    code = runners[0].code

    sev = _weather_severity(weather, track_condition=track_condition)

    # priors by code (then adjusted by conditions)
    if code == "greyhound":
        prior = {"box": 0.25, "form": 0.45, "early": 0.30}
    elif code == "thoroughbred":
        prior = {"box": 0.20, "form": 0.50, "early": 0.30}  # early == class/weight proxy
    else:  # harness
        prior = {"box": 0.25, "form": 0.45, "early": 0.30}  # early == class/weight proxy (often sparse)

    # Weather/conditions shift: draw more important, proxy less stable.
    if sev > 0:
        prior = {
            "box": prior["box"] * (1.0 + 0.35 * sev),
            "form": prior["form"] * (1.0 + 0.10 * sev),
            "early": prior["early"] * (1.0 - 0.30 * sev),
        }

    n = len(runners)

    def coverage(frac: float) -> float:
        # Keep a little weight even if sparse, but penalize heavily as it approaches 0.
        return 0.20 + 0.80 * clamp01(frac)

    # availability
    draw_cov = sum(1 for r in runners if r.draw is not None) / n
    if code == "greyhound":
        early_cov = sum(1 for r in runners if r.early_speed is not None) / n
        form_cov = sum(1 for r in runners if len(r.recent_finishes or []) >= 3) / n
    else:
        # "early" proxy = benchmark/weight presence
        early_cov = sum(1 for r in runners if (r.benchmark is not None or r.weight_kg is not None)) / n
        form_cov = sum(1 for r in runners if len(r.recent_finishes or []) >= 3) / n

    # variability/informativeness (rough)
    def stdev(vals: list[float]) -> float:
        if len(vals) < 3:
            return 0.0
        m = sum(vals) / len(vals)
        v = sum((x - m) ** 2 for x in vals) / (len(vals) - 1)
        return v ** 0.5

    form_avgs: list[float] = []
    for r in runners:
        xs = [x for x in (r.recent_finishes or [])[:5] if isinstance(x, int) and x > 0]
        if xs:
            form_avgs.append(sum(xs) / len(xs))
    form_var = stdev(form_avgs)

    if code == "greyhound":
        early_vals = [float(r.early_speed) for r in runners if r.early_speed is not None]
        # higher stdev = more differentiation (good); very low stdev => downweight
        early_var = stdev(early_vals)
    else:
        bm_vals = [float(r.benchmark) for r in runners if r.benchmark is not None]
        wt_vals = [float(r.weight_kg) for r in runners if r.weight_kg is not None]
        early_var = max(stdev(bm_vals), stdev(wt_vals))

    # Convert stdev into [0,1] multiplier (soft)
    # These are intentionally gentle so weights don't swing wildly.
    form_info = clamp01(form_var / 2.0)  # avg finishes spread by ~2 is "informative"
    early_info = clamp01(early_var / (0.25 if code == "greyhound" else 2.0))

    w_box = prior["box"] * coverage(draw_cov)
    w_form = prior["form"] * coverage(form_cov) * (0.50 + 0.50 * form_info)
    w_early = prior["early"] * coverage(early_cov) * (0.50 + 0.50 * early_info)

    bw, fw, ew = normalize_weights(w_box, w_form, w_early)

    rationale = [
        f"Auto weights chosen for code={code}.",
        f"Conditions severity≈{sev:.2f} (from weather/track if available).",
        f"Signal coverage: draw={draw_cov:.0%}, form={form_cov:.0%}, proxy={early_cov:.0%}.",
        f"Signal spread: form_stdev≈{form_var:.2f}, proxy_spread≈{early_var:.2f}.",
        f"Final weights: draw={bw:.2f}, form={fw:.2f}, proxy={ew:.2f}.",
    ]
    return (bw, fw, ew, rationale)


def score_box_advantage(box: Optional[int]) -> Optional[float]:
    """
    Generic inside bias: boxes 1–3 slightly favored.
    Returns a score in [0,1], higher is better.
    """
    if box is None:
        return None
    if box in (1, 2, 3):
        return 1.0
    if box in (4, 5, 6):
        return 0.7
    if box in (7, 8):
        return 0.4
    return 0.5


def score_recent_form(recent_finishes: list[int]) -> Optional[float]:
    """
    Average of last 5 finishes (lower is better) -> normalized to [0,1].
    We map avg finish 1 -> 1.0, avg finish 8 -> ~0.0 (clamped).
    """
    if not recent_finishes:
        return None
    vals = [v for v in recent_finishes[:5] if isinstance(v, int) and v > 0]
    if not vals:
        return None
    avg = sum(vals) / len(vals)
    # Simple linear mapping
    return clamp01((8.0 - avg) / 7.0)


def score_early_speed_proxy(early_speed: Optional[float], *, min_good: float = 4.8, max_bad: float = 6.5) -> Optional[float]:
    """
    If early_speed looks like a split time in seconds, lower is better.
    Normalize into [0,1] where <=min_good => 1.0 and >=max_bad => 0.0.
    """
    if early_speed is None:
        return None
    x = float(early_speed)
    if x <= min_good:
        return 1.0
    if x >= max_bad:
        return 0.0
    return clamp01((max_bad - x) / (max_bad - min_good))


def score_recent_form_horseish(recent_finishes: list[int]) -> Optional[float]:
    """
    Horses/harness: map average finish (lower better) into [0,1].
    Use a looser scale than greyhounds.
    """
    if not recent_finishes:
        return None
    vals = [v for v in recent_finishes[:6] if isinstance(v, int) and v > 0]
    if not vals:
        return None
    avg = sum(vals) / len(vals)
    # avg 1 => 1.0, avg 10 => 0.0
    return clamp01((10.0 - avg) / 9.0)


def score_barrier_inside(barrier: Optional[int], field_size: Optional[int]) -> Optional[float]:
    if barrier is None or not field_size or field_size <= 1:
        return None
    # Map barrier in [1,field_size] to [1.0,0.4]
    return clamp01(1.0 - (max(0, barrier - 1) / max(1, field_size - 1)) * 0.6)


def score_benchmark_relative(benchmark: Optional[float], field_bms: list[float]) -> Optional[float]:
    if benchmark is None or not field_bms:
        return None
    lo = min(field_bms)
    hi = max(field_bms)
    if hi <= lo:
        return None
    return clamp01((float(benchmark) - float(lo)) / (float(hi) - float(lo)))


def score_weight_relative(weight_kg: Optional[float], field_weights: list[float]) -> Optional[float]:
    if weight_kg is None or not field_weights:
        return None
    lo = min(field_weights)
    hi = max(field_weights)
    if hi <= lo:
        return None
    # lower is better
    return 1.0 - clamp01((float(weight_kg) - float(lo)) / (float(hi) - float(lo)))


@dataclass(frozen=True)
class RankedRunner:
    rank: int
    name: str
    draw: Optional[int]
    draw_label: str
    score: float
    key_factors: str
    why_bullets: list[str]
    debug: dict[str, Any]


def _factor_sentence(label: str, strength: float) -> str:
    if strength >= 0.85:
        return f"{label} (strong)"
    if strength >= 0.65:
        return f"{label} (good)"
    if strength >= 0.45:
        return f"{label} (ok)"
    return f"{label} (weak)"


def rank_runners(
    runners: list[Runner],
    *,
    box_weight: float,
    form_weight: float,
    early_weight: float,
    weather: Optional[WeatherSnapshot] = None,
    track_condition: Optional[str] = None,
    explain_mode: str = "short",  # "short" | "detailed"
) -> list[RankedRunner]:
    bw, fw, ew = normalize_weights(box_weight, form_weight, early_weight)

    code = runners[0].code if runners else "greyhound"
    sev = _weather_severity(weather, track_condition=track_condition)

    scored: list[tuple[float, dict[str, Optional[float]], Runner]] = []
    for r in runners:
        # Greyhounds: use split-time early speed proxy
        if r.code == "greyhound":
            s_box = score_box_advantage(r.draw)
            s_form = score_recent_form(r.recent_finishes)
            s_early = score_early_speed_proxy(r.early_speed)
        else:
            field_size = max([x.draw for x in runners if x.draw is not None], default=None)
            field_weights = [float(x.weight_kg) for x in runners if x.weight_kg is not None]
            field_bms = [float(x.benchmark) for x in runners if x.benchmark is not None]

            s_box = score_barrier_inside(r.draw, field_size)
            s_form = score_recent_form_horseish(r.recent_finishes) or score_recent_form(r.recent_finishes)

            bm_score = score_benchmark_relative(r.benchmark, field_bms)
            wt_score = score_weight_relative(r.weight_kg, field_weights)
            # Treat early_weight as combined "class/weight proxy"
            if bm_score is not None and wt_score is not None:
                s_early = (bm_score + wt_score) / 2.0
            else:
                s_early = bm_score if bm_score is not None else wt_score

        # Conditions adjustments:
        # - inside draws become slightly more valuable
        # - early speed/class proxies become less reliable (compress toward neutral)
        if sev > 0:
            if s_box is not None and r.draw is not None:
                if r.code == "greyhound":
                    if r.draw <= 3:
                        s_box = clamp01(s_box + 0.12 * sev)
                    elif r.draw >= 7:
                        s_box = clamp01(s_box - 0.12 * sev)
                else:
                    # normalize draw to inside/outside effect
                    # if draw=1 => +, if draw near max => -
                    max_draw = max([x.draw for x in runners if x.draw is not None], default=r.draw)
                    if max_draw and max_draw > 1:
                        inside01 = 1.0 - ((r.draw - 1) / (max_draw - 1))
                        s_box = clamp01(s_box + (inside01 - 0.5) * 0.25 * sev)

            if s_early is not None:
                s_early = 0.5 + (s_early - 0.5) * (1.0 - 0.55 * sev)

        # Weighted average over available components only (skip missing gracefully).
        total_w = 0.0
        total = 0.0
        if s_box is not None:
            total += bw * s_box
            total_w += bw
        if s_form is not None:
            total += fw * s_form
            total_w += fw
        if s_early is not None:
            total += ew * s_early
            total_w += ew
        final = (total / total_w) if total_w > 0 else 0.5

        scored.append((final, {"box": s_box, "form": s_form, "early": s_early}, r))

    scored.sort(key=lambda x: x[0], reverse=True)

    ranked: list[RankedRunner] = []
    for i, (final, parts, r) in enumerate(scored, start=1):
        factors: list[tuple[str, float]] = []
        if parts["box"] is not None:
            factors.append((("inside box bias" if r.code == "greyhound" else "inside draw bias"), float(parts["box"])))
        if parts["form"] is not None:
            factors.append(("recent form", float(parts["form"])))
        if parts["early"] is not None:
            factors.append((("early speed proxy" if r.code == "greyhound" else "class/weight proxy"), float(parts["early"])))
        factors.sort(key=lambda x: x[1], reverse=True)

        top = factors[:2] if factors else []
        key = ", ".join(_factor_sentence(lbl, val) for lbl, val in top) if top else "limited public data"

        bullets: list[str] = []
        if factors:
            for lbl, val in factors[:3]:
                if explain_mode == "detailed":
                    bullets.append(f"{lbl}: contributes {val:.2f} to this runner’s profile.")
                else:
                    bullets.append(f"{lbl}: {_factor_sentence('signal', val)}.")
        else:
            bullets.append("Public card data was sparse; score is conservative.")

        ranked.append(
            RankedRunner(
                rank=i,
                name=r.name,
                draw=r.draw,
                draw_label=("box" if r.code == "greyhound" else ("barrier" if r.code == "thoroughbred" else "gate")),
                score=clamp01(final),
                key_factors=key,
                why_bullets=bullets,
                debug={
                    "parts": parts,
                    "weights": {"box": bw, "form": fw, "early": ew},
                    "conditions": {
                        "severity": sev,
                        "track_condition": track_condition,
                        "weather_for_time_iso": getattr(weather, "for_time_iso", None) if weather else None,
                    },
                    "raw": r.raw,
                },
            )
        )

    return ranked

