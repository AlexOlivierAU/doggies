from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Optional

from fetch import get


@dataclass(frozen=True)
class WeatherSnapshot:
    provider: str
    fetched_at_iso: str
    for_time_iso: Optional[str]
    temperature_c: Optional[float]
    relative_humidity_pct: Optional[float]
    precipitation_mm: Optional[float]
    wind_speed_kmh: Optional[float]
    weather_code: Optional[int]


def open_meteo_current_weather(lat: float, lon: float) -> WeatherSnapshot:
    """
    Free, no-auth weather snapshot. Public endpoint.
    Docs: https://open-meteo.com/
    """
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        "&current=temperature_2m,relative_humidity_2m,precipitation,weather_code,wind_speed_10m"
        "&timezone=auto"
    )
    txt = get(url, ttl_seconds=300, timeout_seconds=20).text
    # Avoid adding a json dependency import footprint elsewhere; parse via stdlib
    import json

    data = json.loads(txt)
    cur = data.get("current") or {}
    return WeatherSnapshot(
        provider="open-meteo",
        fetched_at_iso=datetime.now().astimezone().isoformat(timespec="seconds"),
        for_time_iso=cur.get("time"),
        temperature_c=cur.get("temperature_2m"),
        relative_humidity_pct=cur.get("relative_humidity_2m"),
        precipitation_mm=cur.get("precipitation"),
        wind_speed_kmh=cur.get("wind_speed_10m"),
        weather_code=cur.get("weather_code"),
    )

def open_meteo_geocode_au(query: str) -> Optional[tuple[float, float, str]]:
    """
    Resolve a venue name to (lat, lon, resolved_name) using Open-Meteo geocoding.
    Cached by fetch.py disk cache.
    """
    if not query:
        return None
    import json

    q = query.strip()
    url = (
        "https://geocoding-api.open-meteo.com/v1/search"
        f"?name={q}"
        "&count=1"
        "&language=en"
        "&format=json"
        "&country=AU"
    )
    txt = get(url, ttl_seconds=30 * 24 * 3600, timeout_seconds=15).text
    data = json.loads(txt)
    res = (data.get("results") or [])
    if not res:
        return None
    r0 = res[0]
    try:
        lat = float(r0["latitude"])
        lon = float(r0["longitude"])
    except Exception:
        return None
    name = str(r0.get("name") or q)
    admin1 = r0.get("admin1")
    if admin1:
        name = f"{name}, {admin1}"
    return (lat, lon, name)


def open_meteo_forecast_hourly(lat: float, lon: float, on_date: date) -> dict:
    """
    Hourly forecast for a specific date (local timezone).
    """
    import json

    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        f"&start_date={on_date.isoformat()}&end_date={on_date.isoformat()}"
        "&hourly=temperature_2m,relative_humidity_2m,precipitation,weather_code,wind_speed_10m"
        "&timezone=auto"
    )
    txt = get(url, ttl_seconds=300, timeout_seconds=20).text
    return json.loads(txt)


def open_meteo_weather_at_time(lat: float, lon: float, when_local: datetime) -> WeatherSnapshot:
    """
    Pick the nearest-hour forecast snapshot for the given local datetime.
    """
    data = open_meteo_forecast_hourly(lat, lon, when_local.date())
    hourly = data.get("hourly") or {}
    times = hourly.get("time") or []
    if not times:
        # fallback to current if hourly missing
        cur = open_meteo_current_weather(lat, lon)
        return WeatherSnapshot(**{**cur.__dict__, "for_time_iso": when_local.isoformat(timespec="minutes")})

    # Parse ISO times; Open-Meteo hourly timestamps are typically local-time strings
    # without an explicit offset. Compare using naive local times.
    target = when_local.replace(minute=0, second=0, microsecond=0)
    target_naive = target.replace(tzinfo=None)
    best_i = 0
    best_dt = None
    for i, ts in enumerate(times):
        try:
            dt = datetime.fromisoformat(ts)
        except Exception:
            continue
        if best_dt is None or abs((dt - target_naive).total_seconds()) < abs((best_dt - target_naive).total_seconds()):
            best_dt = dt
            best_i = i

    def at(key: str):
        arr = hourly.get(key) or []
        if 0 <= best_i < len(arr):
            return arr[best_i]
        return None

    return WeatherSnapshot(
        provider="open-meteo",
        fetched_at_iso=datetime.now().astimezone().isoformat(timespec="seconds"),
        for_time_iso=str(times[best_i]) if best_i < len(times) else target.isoformat(timespec="minutes"),
        temperature_c=at("temperature_2m"),
        relative_humidity_pct=at("relative_humidity_2m"),
        precipitation_mm=at("precipitation"),
        wind_speed_kmh=at("wind_speed_10m"),
        weather_code=at("weather_code"),
    )


# v0: minimal mapping for common NSW/ACT venues we already use in examples.
# You can expand this over time.
VENUE_LATLON: dict[str, tuple[float, float]] = {
    # Thoroughbred / ACT
    "Canberra": (-35.3106, 149.1333),
    # Greyhounds/horses
    "Newcastle": (-32.9283, 151.7817),
    "Wentworth Park": (-33.8790, 151.1788),
    "Bendigo": (-36.7570, 144.2794),
    "Wagga": (-35.1167, 147.3667),
    "Wagga at Riverina Paceway": (-35.1167, 147.3667),
}


def venue_latlon(venue: str) -> Optional[tuple[float, float]]:
    if not venue:
        return None
    if venue in VENUE_LATLON:
        return VENUE_LATLON[venue]
    # simple fuzzy contains
    for k, (lat, lon) in VENUE_LATLON.items():
        if k.lower() in venue.lower():
            return (lat, lon)
    g = open_meteo_geocode_au(venue)
    if g:
        lat, lon, _ = g
        return (lat, lon)
    # try a slightly more specific query
    g = open_meteo_geocode_au(f"{venue} racecourse")
    if g:
        lat, lon, _ = g
        return (lat, lon)
    return None


def venue_weather(venue: str) -> Optional[WeatherSnapshot]:
    ll = venue_latlon(venue)
    if not ll:
        return None
    lat, lon = ll
    return open_meteo_current_weather(lat, lon)


def venue_weather_for_race(venue: str, meeting_date: date, start_time_local: Optional[time]) -> Optional[WeatherSnapshot]:
    ll = venue_latlon(venue)
    if not ll:
        return None
    lat, lon = ll
    if start_time_local is None:
        return open_meteo_current_weather(lat, lon)
    when_local = datetime.combine(meeting_date, start_time_local).astimezone()
    return open_meteo_weather_at_time(lat, lon, when_local)

