"""Denizli Başkarcı için Open-Meteo hava durumu ve güneş/soğuma tahmini servisi."""

import os
import time
from dataclasses import dataclass
from typing import Optional

import httpx

DEFAULT_LAT = 37.760
DEFAULT_LON = 29.020
CACHE_TTL_SECONDS = 600


@dataclass(frozen=True)
class WeatherSnapshot:
    outside_temp_c: Optional[float]
    min_next_3h_temp_c: Optional[float]
    cloud_cover_pct: Optional[float]
    current_solar_wm2: Optional[float]
    max_next_3h_solar_wm2: Optional[float]
    source: str


_cached_snapshot: Optional[WeatherSnapshot] = None
_cached_at: float = 0.0


def fetch_outdoor_weather(force: bool = False) -> WeatherSnapshot:
    """Denizli Başkarcı (1045 Sok. bölgesi) anlık ve 3 saatlik öngörü verisini çeker."""
    global _cached_snapshot, _cached_at
    now_mono = time.monotonic()
    if not force and _cached_snapshot is not None and (now_mono - _cached_at) < CACHE_TTL_SECONDS:
        return _cached_snapshot

    lat = float(os.getenv("WEATHER_LAT", str(DEFAULT_LAT)))
    lon = float(os.getenv("WEATHER_LON", str(DEFAULT_LON)))

    try:
        resp = httpx.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat,
                "longitude": lon,
                "current": "temperature_2m,cloud_cover,shortwave_radiation",
                "hourly": "temperature_2m,shortwave_radiation",
                "forecast_hours": 4,
                "timezone": "Europe/Istanbul",
            },
            timeout=8.0,
        )
        resp.raise_for_status()
        data = resp.json()

        current = data.get("current", {})
        hourly = data.get("hourly", {})
        temps = [float(t) for t in (hourly.get("temperature_2m") or [])[:4] if t is not None]
        solars = [float(s) for s in (hourly.get("shortwave_radiation") or [])[:4] if s is not None]

        snap = WeatherSnapshot(
            outside_temp_c=float(current["temperature_2m"]) if current.get("temperature_2m") is not None else None,
            min_next_3h_temp_c=min(temps) if temps else None,
            cloud_cover_pct=float(current["cloud_cover"]) if current.get("cloud_cover") is not None else None,
            current_solar_wm2=float(current["shortwave_radiation"]) if current.get("shortwave_radiation") is not None else None,
            max_next_3h_solar_wm2=max(solars) if solars else None,
            source="open-meteo-baskarcı",
        )
        _cached_snapshot = snap
        _cached_at = now_mono
        return snap
    except Exception:
        if _cached_snapshot is not None:
            return _cached_snapshot
        return WeatherSnapshot(
            outside_temp_c=None,
            min_next_3h_temp_c=None,
            cloud_cover_pct=None,
            current_solar_wm2=None,
            max_next_3h_solar_wm2=None,
            source="weather_unavailable",
        )
