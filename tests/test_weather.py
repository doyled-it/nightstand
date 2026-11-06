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
