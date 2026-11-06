from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

from PIL import Image, ImageDraw

from nightstand.models import NowPlaying
from nightstand.palette import PURE
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


def _count_color(img: Image.Image, box: tuple[int, int, int, int], color) -> int:
    """How many pixels in ``box`` equal ``color`` after the six-color snap."""
    crop = img.convert("RGB").crop(box)
    colors = crop.getcolors(maxcolors=crop.width * crop.height) or []
    return sum(count for count, pixel in colors if pixel == color)


def test_weather_render_skips_sun_bar_when_times_missing():
    # A usable forecast can still be missing sunrise/sunset; the bar is skipped
    # cleanly rather than crashing in the clock formatter.
    f = replace(_forecast(), sunrise=None, sunset=None)
    img = weather.render(f, _now())

    assert img.size == (WIDTH, HEIGHT)
    # Everything below the table is blank: no bar, no clock labels.
    band = (0, weather.TABLE_BOTTOM + 2, WIDTH, HEIGHT)
    assert _count_color(img, band, PURE["white"]) == (
        (band[2] - band[0]) * (band[3] - band[1])
    )


def test_split_render_skips_uv_marker_when_peak_time_missing():
    f = replace(_forecast(), uv_peak=0, uv_peak_time=None)
    np = NowPlaying(title="Kind of Blue", artist="Miles Davis", rooms=("Office",))
    img = split.render(f, np, None, _now())

    assert img.size == (WIDTH, HEIGHT)
    # No UV marker means no red block on the high/low line.
    header = (WIDTH // 2, 90, WIDTH, 150)
    assert _count_color(img, header, PURE["red"]) == 0


def test_split_render_long_rooms_keeps_title_and_stays_in_bounds():
    rooms = (
        "Primary Bedroom",
        "Downstairs Living Room",
        "Upstairs Guest Suite",
        "Kitchen and Dining",
        "Back Garden Patio",
        "Home Office Studio",
    )
    np = NowPlaying(
        title="A Considerably Long Song Title", artist="A Long Band Name", rooms=rooms
    )
    img = split.render(_forecast(), np, None, _now())

    assert img.size == (WIDTH, HEIGHT)
    # The title keeps a readable minimum width: white glyphs reach well past its
    # start rather than collapsing to a lone ellipsis when the room list is long.
    title_x = split.MARGIN + split.COVER_SIZE + split.COVER_GAP
    title_band = (title_x + 120, split.STRIP_TOP + 10, title_x + 230, HEIGHT)
    assert _count_color(img, title_band, PURE["white"]) > 150
    # Nothing is drawn into the right margin: content stays inside the strip.
    right_margin = (WIDTH - split.MARGIN + 1, split.STRIP_TOP, WIDTH, HEIGHT)
    assert _count_color(img, right_margin, PURE["white"]) == 0
