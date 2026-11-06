#!/usr/bin/env python3
"""Record an Open-Meteo forecast snapshot as a test fixture.

The weather screen parses a forecast from ``/v1/forecast``. This script fetches
one real response for a public city center, then strips every location-revealing
field before saving: latitude, longitude, elevation, and the timezone name,
abbreviation, and offset. The household's real coordinates never go through this
script and never land in the repo. Pass any well-known public city center on the
command line; the saved JSON keeps only the keys the parser reads, so the city it
came from is not recoverable from the fixture.

    uv run python scripts/record_openmeteo_fixture.py \
        --latitude 41.8781 --longitude -87.6298 \
        --out tests/fixtures/openmeteo/forecast.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import httpx

_FORECAST = "https://api.open-meteo.com/v1/forecast"
_TIMEOUT = 30.0


def fetch(client: httpx.Client, latitude: float, longitude: float) -> dict:
    """Fetch one day's forecast for a public city center."""
    response = client.get(
        _FORECAST,
        params={
            "latitude": latitude,
            "longitude": longitude,
            "current": "temperature_2m,weather_code",
            "hourly": "temperature_2m,precipitation_probability,uv_index",
            "daily": "temperature_2m_max,temperature_2m_min,sunrise,sunset",
            "temperature_unit": "fahrenheit",
            "timezone": "auto",
            "forecast_days": 1,
        },
        timeout=_TIMEOUT,
    )
    response.raise_for_status()
    return response.json()


def strip_location(data: dict) -> dict:
    """Keep only the keys the parser reads, dropping all location-revealing ones.

    Latitude, longitude, elevation, and the timezone name, abbreviation, and
    offset are all removed. The parser takes the timezone from the caller's
    ``now`` rather than the response, so none of these are needed to render.
    """
    current = data.get("current", {})
    hourly = data.get("hourly", {})
    daily = data.get("daily", {})
    return {
        "current": {
            "temperature_2m": current.get("temperature_2m"),
            "weather_code": current.get("weather_code"),
        },
        "hourly": {
            "time": hourly.get("time", []),
            "temperature_2m": hourly.get("temperature_2m", []),
            "precipitation_probability": hourly.get("precipitation_probability", []),
            "uv_index": hourly.get("uv_index", []),
        },
        "daily": {
            "temperature_2m_max": daily.get("temperature_2m_max", []),
            "temperature_2m_min": daily.get("temperature_2m_min", []),
            "sunrise": daily.get("sunrise", []),
            "sunset": daily.get("sunset", []),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--latitude", type=float, required=True)
    parser.add_argument("--longitude", type=float, required=True)
    parser.add_argument(
        "--out", type=Path, default=Path("tests/fixtures/openmeteo/forecast.json")
    )
    args = parser.parse_args(argv)

    with httpx.Client() as client:
        data = fetch(client, args.latitude, args.longitude)
    trimmed = strip_location(data)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(trimmed, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
