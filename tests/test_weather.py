from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from nightstand.sources import weather
from nightstand.sources.weather import Forecast, HourForecast

FIXTURES = Path(__file__).parent / "fixtures" / "openmeteo"

# A fixed zone for the explicit aware datetimes the parser is given, so the test
# never depends on the machine's local timezone. The fixture's own timezone was
# stripped when it was recorded; the parser takes the zone from ``now`` instead.
TZ = timezone(timedelta(hours=-6))


def load() -> dict:
    return json.loads((FIXTURES / "forecast.json").read_text())


def _response(
    *,
    hours: list[dict],
    current=42.6,
    code=3,
    high=45.5,
    low=34.6,
    sunrise="2026-11-06T06:29",
    sunset="2026-11-06T16:38",
) -> dict:
    """A minimal Open-Meteo response shaped from inline hour specs.

    Each hour spec is ``{"time", "temp", "rain", "uv"}``; any of ``temp``,
    ``rain``, ``uv`` may be ``None`` to model a gap in the response.
    """
    return {
        "current": {"temperature_2m": current, "weather_code": code},
        "hourly": {
            "time": [h["time"] for h in hours],
            "temperature_2m": [h["temp"] for h in hours],
            "precipitation_probability": [h["rain"] for h in hours],
            "uv_index": [h["uv"] for h in hours],
        },
        "daily": {
            "temperature_2m_max": [high],
            "temperature_2m_min": [low],
            "sunrise": [sunrise],
            "sunset": [sunset],
        },
    }


def _full_day(temp=60, rain=0, uv=1) -> list[dict]:
    """Twenty-four hour specs for 2026-11-06, each with the same default values."""
    return [
        {"time": f"2026-11-06T{h:02d}:00", "temp": temp, "rain": rain, "uv": uv}
        for h in range(24)
    ]


# --- fetch_forecast --------------------------------------------------------


def test_fetch_forecast_builds_the_open_meteo_request():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = request.url
        return httpx.Response(200, json={"ok": True})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    data = weather.fetch_forecast(client, 12.5, -34.25, "America/Los_Angeles")

    assert data == {"ok": True}
    url = captured["url"]
    assert url.host == "api.open-meteo.com"
    assert url.path == "/v1/forecast"
    params = url.params
    assert params["latitude"] == "12.5"
    assert params["longitude"] == "-34.25"
    assert params["current"] == "temperature_2m,weather_code"
    assert params["hourly"] == "temperature_2m,precipitation_probability,uv_index"
    assert (
        params["daily"] == "temperature_2m_max,temperature_2m_min,sunrise,sunset"
    )
    assert params["temperature_unit"] == "fahrenheit"
    assert params["timezone"] == "America/Los_Angeles"
    assert params["forecast_days"] == "1"


def test_fetch_forecast_defaults_timezone_to_auto():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = request.url
        return httpx.Response(200, json={})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    weather.fetch_forecast(client, 0.0, 0.0, None)

    assert captured["url"].params["timezone"] == "auto"


# --- parse_forecast --------------------------------------------------------


def test_parse_forecast_gives_five_hours_at_two_hour_steps():
    now = datetime(2026, 11, 6, 6, 30, tzinfo=TZ)
    f = weather.parse_forecast(load(), now)

    assert len(f.hours) == 5
    # The first step is the next whole hour after 06:30, then every 2 hours.
    assert [h.time.hour for h in f.hours] == [7, 9, 11, 13, 15]
    assert all(h.time.tzinfo == TZ for h in f.hours)
    # Values come from the fixture's hourly arrays, rounded.
    assert [h.temp for h in f.hours] == [35, 38, 41, 45, 45]
    assert [h.rain_pct for h in f.hours] == [0, 0, 0, 0, 0]
    assert [h.uv for h in f.hours] == [0, 1, 2, 3, 2]


def test_parse_forecast_reads_current_daily_and_uv_peak():
    now = datetime(2026, 11, 6, 6, 30, tzinfo=TZ)
    f = weather.parse_forecast(load(), now)

    assert f.now_temp == 43
    assert f.description == "Overcast"
    assert f.high == 46
    assert f.low == 35
    assert f.sunrise == datetime(2026, 11, 6, 6, 29, tzinfo=TZ)
    assert f.sunset == datetime(2026, 11, 6, 16, 38, tzinfo=TZ)
    # Peak UV across the day is 2.95 at 12:00, rounded to 3.
    assert f.uv_peak == 3
    assert f.uv_peak_time == datetime(2026, 11, 6, 12, 0, tzinfo=TZ)


def test_parse_forecast_on_the_hour_starts_at_that_hour():
    now = datetime(2026, 11, 6, 8, 0, tzinfo=TZ)
    f = weather.parse_forecast(load(), now)

    assert [h.time.hour for h in f.hours] == [8, 10, 12, 14, 16]


# --- parse_forecast: rain and the umbrella rule ----------------------------


def test_parse_forecast_pins_rain_pct_per_hour():
    hours = _full_day()
    for hour, rain in {7: 10, 9: 30, 11: 55, 13: 20, 15: 0}.items():
        hours[hour]["rain"] = rain
    now = datetime(2026, 11, 6, 6, 30, tzinfo=TZ)

    f = weather.parse_forecast(_response(hours=hours), now)

    assert [h.rain_pct for h in f.hours] == [10, 30, 55, 20, 0]
    # Any shown hour at 50 percent or more adds the umbrella.
    assert weather.what_to_wear(f) == "Warm layers weather + umbrella"


def test_parse_forecast_no_umbrella_when_rain_stays_below_fifty():
    hours = _full_day(rain=49)
    now = datetime(2026, 11, 6, 6, 30, tzinfo=TZ)

    f = weather.parse_forecast(_response(hours=hours), now)

    assert all(h.rain_pct == 49 for h in f.hours)
    assert "umbrella" not in weather.what_to_wear(f)


# --- parse_forecast: missing data ------------------------------------------


def test_parse_forecast_returns_none_when_current_temp_missing():
    response = _response(hours=_full_day(), current=None)
    now = datetime(2026, 11, 6, 6, 30, tzinfo=TZ)

    assert weather.parse_forecast(response, now) is None


def test_parse_forecast_returns_none_when_high_or_low_missing():
    now = datetime(2026, 11, 6, 6, 30, tzinfo=TZ)

    no_high = _response(hours=_full_day(), high=None)
    no_low = _response(hours=_full_day(), low=None)

    assert weather.parse_forecast(no_high, now) is None
    assert weather.parse_forecast(no_low, now) is None


def test_parse_forecast_usable_when_only_sun_times_missing():
    response = _response(hours=_full_day(), sunrise=None, sunset=None)
    now = datetime(2026, 11, 6, 6, 30, tzinfo=TZ)

    f = weather.parse_forecast(response, now)

    assert f is not None
    assert f.sunrise is None
    assert f.sunset is None
    # A usable forecast still has its temperatures.
    assert f.now_temp == 43
    assert f.high == 46
    assert f.low == 35


def test_parse_forecast_drops_hour_with_missing_temperature():
    hours = _full_day()
    hours[9]["temp"] = None  # the second shown step (09:00) has no temperature
    now = datetime(2026, 11, 6, 6, 30, tzinfo=TZ)

    f = weather.parse_forecast(_response(hours=hours), now)

    # The 09:00 column is dropped rather than shown as a fake 0 degrees.
    assert [h.time.hour for h in f.hours] == [7, 11, 13, 15]


def test_parse_forecast_keeps_hour_but_dashes_missing_rain_and_uv():
    hours = _full_day()
    hours[7]["rain"] = None
    hours[7]["uv"] = None
    now = datetime(2026, 11, 6, 6, 30, tzinfo=TZ)

    f = weather.parse_forecast(_response(hours=hours), now)

    first = f.hours[0]
    assert first.time.hour == 7
    assert first.temp == 60  # temperature is present, so the hour stays
    assert first.rain_pct is None  # missing values become None, never 0
    assert first.uv is None


def test_parse_forecast_uv_peak_time_none_when_no_uv_readings():
    hours = [
        {"time": h["time"], "temp": h["temp"], "rain": h["rain"], "uv": None}
        for h in _full_day()
    ]
    now = datetime(2026, 11, 6, 6, 30, tzinfo=TZ)

    f = weather.parse_forecast(_response(hours=hours), now)

    assert f.uv_peak == 0
    assert f.uv_peak_time is None


# --- describe --------------------------------------------------------------


def test_describe_maps_wmo_codes_to_phrases():
    assert weather.describe(0) == "Clear"
    assert weather.describe(2) == "Partly cloudy"
    assert weather.describe(3) == "Overcast"
    assert weather.describe(45) == "Fog"
    assert weather.describe(53) == "Drizzle"
    assert weather.describe(63) == "Rain"
    assert weather.describe(71) == "Snow"
    assert weather.describe(81) == "Showers"
    assert weather.describe(95) == "Thunderstorms"


def test_describe_falls_back_for_an_unknown_code():
    # An unmapped code should still yield a usable phrase rather than raise.
    assert weather.describe(999) != ""


# --- what_to_wear ----------------------------------------------------------


def _forecast(
    now_temp: int, high: int, hours: tuple[HourForecast, ...] = ()
) -> Forecast:
    base = datetime(2026, 11, 6, 7, 0, tzinfo=TZ)
    return Forecast(
        now_temp=now_temp,
        description="Clear",
        high=high,
        low=high - 10,
        hours=hours,
        sunrise=datetime(2026, 11, 6, 6, 29, tzinfo=TZ),
        sunset=datetime(2026, 11, 6, 16, 38, tzinfo=TZ),
        uv_peak=3,
        uv_peak_time=base,
    )


def test_what_to_wear_changes_bucket_across_the_day():
    assert weather.what_to_wear(_forecast(61, 72)) == "Jacket now, T-shirt later"


def test_what_to_wear_same_bucket():
    assert weather.what_to_wear(_forecast(68, 72)) == "T-shirt weather"


def test_what_to_wear_appends_umbrella_on_high_rain():
    hour = HourForecast(
        time=datetime(2026, 11, 6, 9, 0, tzinfo=TZ), temp=51, rain_pct=60, uv=1
    )
    assert (
        weather.what_to_wear(_forecast(50, 52, (hour,)))
        == "Warm layers weather + umbrella"
    )


def test_what_to_wear_buckets_cover_every_range():
    assert weather.what_to_wear(_forecast(40, 40)) == "Warm layers weather"
    assert weather.what_to_wear(_forecast(60, 60)) == "Jacket weather"
    assert weather.what_to_wear(_forecast(70, 70)) == "T-shirt weather"
    assert weather.what_to_wear(_forecast(80, 80)) == "Shorts weather"
