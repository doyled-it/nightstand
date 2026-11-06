"""The morning forecast from Open-Meteo.

Open-Meteo's forecast API needs no key or account. ``fetch_forecast`` makes the
one request the screens need, and ``parse_forecast`` reduces the response to the
one ``Forecast`` the weather and split screens draw: the current temperature, a
short description, the day's high and low, five hourly steps, sunrise and sunset,
and the day's peak UV.

The response carries no location: the recorded fixture has latitude, longitude,
elevation, and the timezone name and offset stripped. The hourly, sunrise, and
sunset times are naive local strings; ``parse_forecast`` reads the timezone from
the caller's ``now`` rather than from the response or the machine, so the app
controls the zone through the one aware datetime it passes in.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx

_FORECAST = "https://api.open-meteo.com/v1/forecast"
_TIMEOUT = 10.0

# How many hourly steps the table shows, and the gap between them in hours.
_HOUR_STEPS = 5
_HOUR_GAP = 2

# WMO weather codes to a short phrase. Codes within a family (the drizzle,
# rain, snow, and shower bands) share a phrase; ``describe`` maps the rest to
# the nearest family so an unmapped code still reads as weather.
_WMO: dict[int, str] = {
    0: "Clear",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Fog",
    51: "Drizzle",
    53: "Drizzle",
    55: "Drizzle",
    56: "Freezing drizzle",
    57: "Freezing drizzle",
    61: "Rain",
    63: "Rain",
    65: "Rain",
    66: "Freezing rain",
    67: "Freezing rain",
    71: "Snow",
    73: "Snow",
    75: "Snow",
    77: "Snow grains",
    80: "Showers",
    81: "Showers",
    82: "Showers",
    85: "Snow showers",
    86: "Snow showers",
    95: "Thunderstorms",
    96: "Thunderstorms",
    99: "Thunderstorms",
}


@dataclass(frozen=True)
class HourForecast:
    """One hourly step in the table: the hour, temperature, rain chance, UV.

    ``rain_pct`` and ``uv`` are ``None`` when the response omits them for that
    hour; the table renders a dash rather than a misleading zero. An hour with no
    temperature is dropped entirely, so ``temp`` is always present.
    """

    time: datetime
    temp: int
    rain_pct: int | None
    uv: int | None


@dataclass(frozen=True)
class Forecast:
    """The day's forecast as the screens draw it.

    ``hours`` holds up to five steps starting at the next whole hour, every two
    hours. ``uv_peak`` is the day's highest UV and ``uv_peak_time`` when it falls,
    which the split screen folds into the header line.

    ``sunrise``, ``sunset``, and ``uv_peak_time`` are ``None`` when the response
    omits them; the renderers skip the sun bar or the UV marker rather than draw
    a placeholder. ``now_temp``, ``high``, and ``low`` are always present: a
    response missing any of them is not a usable forecast, so ``parse_forecast``
    returns ``None`` instead of inventing a value.
    """

    now_temp: int
    description: str
    high: int
    low: int
    hours: tuple[HourForecast, ...]
    sunrise: datetime | None
    sunset: datetime | None
    uv_peak: int
    uv_peak_time: datetime | None


def fetch_forecast(
    client: httpx.Client, lat: float, lon: float, tz: str | None
) -> dict:
    """Fetch one day's forecast for ``lat``/``lon`` in timezone ``tz``.

    ``tz`` is an IANA name, or ``None`` to let Open-Meteo infer the zone from the
    coordinates (``timezone=auto``).
    """
    response = client.get(
        _FORECAST,
        params={
            "latitude": lat,
            "longitude": lon,
            "current": "temperature_2m,weather_code",
            "hourly": "temperature_2m,precipitation_probability,uv_index",
            "daily": "temperature_2m_max,temperature_2m_min,sunrise,sunset",
            "temperature_unit": "fahrenheit",
            "timezone": tz or "auto",
            "forecast_days": 1,
        },
        timeout=_TIMEOUT,
    )
    response.raise_for_status()
    return response.json()


def parse_forecast(data: dict, now: datetime) -> Forecast | None:
    """Reduce an Open-Meteo response to a ``Forecast`` as of ``now``, or ``None``.

    ``now`` is an aware datetime; its timezone is applied to the response's naive
    local times, so the zone comes from the caller, never the machine.

    Returns ``None`` when the current temperature or the day's high or low is
    missing, since a bedside frame should fall through to the next screen rather
    than show a confident wrong forecast. Sunrise, sunset, and per-hour values may
    still be missing on a usable forecast; those are carried as ``None`` and the
    renderers skip them.
    """
    tz = now.tzinfo
    current = data.get("current", {})
    daily = data.get("daily", {})
    hourly = data.get("hourly", {})

    now_temp = current.get("temperature_2m")
    high = _first(daily.get("temperature_2m_max"))
    low = _first(daily.get("temperature_2m_min"))
    if now_temp is None or high is None or low is None:
        return None

    times = [_aware(_parse_local(t), tz) for t in hourly.get("time", [])]
    temps = hourly.get("temperature_2m", [])
    rains = hourly.get("precipitation_probability", [])
    uvs = hourly.get("uv_index", [])

    hours = _pick_hours(now, times, temps, rains, uvs)
    uv_peak, uv_peak_time = _peak_uv(times, uvs)

    return Forecast(
        now_temp=round(now_temp),
        description=describe(current.get("weather_code")),
        high=round(high),
        low=round(low),
        hours=hours,
        sunrise=_aware(_parse_local(_first(daily.get("sunrise"))), tz),
        sunset=_aware(_parse_local(_first(daily.get("sunset"))), tz),
        uv_peak=uv_peak,
        uv_peak_time=uv_peak_time,
    )


def describe(code: int | None) -> str:
    """A short phrase for a WMO weather ``code``.

    An exact match wins; otherwise the code falls back to the nearest lower
    mapped code so an unexpected value in a known band still reads sensibly.
    """
    if code in _WMO:
        return _WMO[code]
    if code is None:
        return "Unknown"
    lower = [c for c in _WMO if c <= code]
    if lower:
        return _WMO[max(lower)]
    return "Unknown"


def what_to_wear(f: Forecast) -> str:
    """A plain dressing line from the current and high temperatures.

    Buckets by temperature. When now and the high fall in different buckets the
    line names both, otherwise one. ``" + umbrella"`` is appended when any shown
    hour has at least a 50 percent chance of rain.
    """
    now_bucket = _wear_bucket(f.now_temp)
    high_bucket = _wear_bucket(f.high)
    if now_bucket != high_bucket:
        line = f"{now_bucket} now, {high_bucket} later"
    else:
        line = f"{now_bucket} weather"
    if any(hour.rain_pct is not None and hour.rain_pct >= 50 for hour in f.hours):
        line += " + umbrella"
    return line


def _wear_bucket(temp: int) -> str:
    if temp < 55:
        return "Warm layers"
    if temp < 65:
        return "Jacket"
    if temp < 75:
        return "T-shirt"
    return "Shorts"


def _pick_hours(
    now: datetime,
    times: list[datetime],
    temps: list,
    rains: list,
    uvs: list,
) -> tuple[HourForecast, ...]:
    """Up to five steps from the next whole hour, every two hours.

    An hour whose temperature is missing is dropped, since there is nothing to
    anchor the column on. A missing rain chance or UV is carried as ``None`` so
    the table can render a dash rather than a misleading zero.
    """
    start = _next_whole_hour(now)
    first = next((i for i, t in enumerate(times) if t >= start), None)
    if first is None:
        return ()
    hours: list[HourForecast] = []
    for step in range(_HOUR_STEPS):
        index = first + step * _HOUR_GAP
        if index >= len(times):
            break
        temp = _at(temps, index)
        if temp is None:
            continue
        hours.append(
            HourForecast(
                time=times[index],
                temp=round(temp),
                rain_pct=_round_or_none(_at(rains, index)),
                uv=_round_or_none(_at(uvs, index)),
            )
        )
    return tuple(hours)


def _peak_uv(times: list[datetime], uvs: list) -> tuple[int, datetime | None]:
    """The day's highest UV, rounded, and the hour it falls on.

    Hours with no UV reading are skipped. With no readings at all the peak is 0
    and its time is ``None``, which the split header treats as no marker.
    """
    best_value: float | None = None
    best_time: datetime | None = None
    for index, time in enumerate(times):
        value = _at(uvs, index)
        if value is None:
            continue
        if best_value is None or value > best_value:
            best_value = value
            best_time = time
    if best_value is None:
        return 0, None
    return round(best_value), best_time


def _next_whole_hour(now: datetime) -> datetime:
    """``now`` rounded up to the next whole hour, or ``now`` if already on one."""
    whole = now.replace(minute=0, second=0, microsecond=0)
    if now != whole:
        whole += timedelta(hours=1)
    return whole


def _parse_local(stamp: str | None) -> datetime | None:
    if not stamp:
        return None
    return datetime.fromisoformat(stamp)


def _aware(value: datetime | None, tz) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=tz)


def _first(values):
    if values:
        return values[0]
    return None


def _at(values: list, index: int):
    if 0 <= index < len(values):
        return values[index]
    return None


def _round_or_none(value) -> int | None:
    if value is None:
        return None
    return round(value)
