from __future__ import annotations

from datetime import datetime, timedelta, timezone

from PIL import Image, ImageDraw

from nightstand.models import NowPlaying
from nightstand.screens import split, weather
from nightstand.screens.common import HEIGHT, WIDTH
from nightstand.sources.weather import Forecast, HourForecast

# Text-heavy screens: large, bold glyphs leave more anti-aliased edge pixels that
# snap to a different side of the six-color threshold across architectures, so
# the golden comparison needs more headroom than the default. See the game and
# now-playing screens, measured in the same range.
TOLERANCE = 0.03

TZ = timezone(timedelta(hours=-7))


def _hours() -> tuple[HourForecast, ...]:
    specs = [
        (7, 64, 10, 2),
        (9, 70, 20, 5),
        (11, 76, 50, 7),
        (13, 80, 60, 8),
        (15, 82, 30, 6),
    ]
    return tuple(
        HourForecast(
            time=datetime(2026, 11, 6, h, 0, tzinfo=TZ),
            temp=temp,
            rain_pct=rain,
            uv=uv,
        )
        for h, temp, rain, uv in specs
    )


def _forecast() -> Forecast:
    return Forecast(
        now_temp=68,
        description="Partly cloudy",
        high=82,
        low=61,
        hours=_hours(),
        sunrise=datetime(2026, 11, 6, 6, 20, tzinfo=TZ),
        sunset=datetime(2026, 11, 6, 19, 40, tzinfo=TZ),
        uv_peak=8,
        uv_peak_time=datetime(2026, 11, 6, 13, 0, tzinfo=TZ),
    )


def _now() -> datetime:
    return datetime(2026, 11, 6, 10, 30, tzinfo=TZ)


def _cover() -> Image.Image:
    img = Image.new("RGB", (300, 300), (0, 0, 255))
    ImageDraw.Draw(img).ellipse([40, 40, 260, 260], fill=(255, 0, 0))
    return img


def test_weather_render_matches_golden(assert_golden):
    img = weather.render(_forecast(), _now())
    assert img.size == (WIDTH, HEIGHT)
    assert_golden(img, "weather.png", TOLERANCE)


def test_split_render_with_cover_matches_golden(assert_golden):
    np = NowPlaying(
        title="Such Great Heights",
        artist="The Postal Service",
        album="Give Up",
        rooms=("Kitchen", "Office"),
    )
    img = split.render(_forecast(), np, _cover(), _now())
    assert img.size == (WIDTH, HEIGHT)
    assert_golden(img, "split_cover.png", TOLERANCE)


def test_split_render_without_cover_matches_golden(assert_golden):
    np = NowPlaying(
        title="Blue Train",
        artist="John Coltrane",
        rooms=("Bedroom",),
    )
    img = split.render(_forecast(), np, None, _now())
    assert img.size == (WIDTH, HEIGHT)
    assert_golden(img, "split_no_cover.png", TOLERANCE)
