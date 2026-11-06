"""The morning weather screen: temperature, a dressing line, hours, and the sun.

The current temperature fills the top left in a huge weight, with the sky
description and the day's high and low beside it. A yellow band carries one plain
"what to wear" line. Below it a small table gives five hourly steps as three rows
(temperature, rain chance, UV), with any UV of 6 or more drawn white on red. A
sunrise-to-sunset bar along the bottom fills with the fraction of daylight that
has passed.

Everything is drawn anti-aliased, then the whole canvas is snapped to the six
pure panel colors, so the panel only ever receives six colors.
"""

from __future__ import annotations

from datetime import datetime

from PIL import Image, ImageDraw

from nightstand.palette import PURE, to_six_colors
from nightstand.screens.common import HEIGHT, WIDTH
from nightstand.sources.weather import Forecast, what_to_wear
from nightstand.text import font

MARGIN = 28

TEMP_SIZE = 170
TEMP_TOP = 2
DESC_SIZE = 36
HILO_SIZE = 28
HEADER_TEXT_X = 340
DESC_TOP = 44
HILO_TOP = 104

WEAR_SIZE = 30
WEAR_TOP = 196
WEAR_HEIGHT = 52

TABLE_TOP = 266
TABLE_BOTTOM = 430
LABEL_COL_WIDTH = 92
ROW_LABEL_SIZE = 22
CELL_SIZE = 30

SUN_LABEL_SIZE = 20
SUN_LABEL_TOP = 436
SUN_BAR_TOP = 460
SUN_BAR_HEIGHT = 16

# UV at or above this reads as a warning and is drawn white on red.
UV_WARN = 6


def render(f: Forecast, now: datetime) -> Image.Image:
    """Render the weather screen for ``f`` as of ``now`` at 800x480."""
    canvas = Image.new("RGB", (WIDTH, HEIGHT), PURE["white"])
    draw = ImageDraw.Draw(canvas)

    draw_header(draw, f, uv_peak=False)
    draw_wear(draw, f, WEAR_TOP)
    draw_table(draw, f, TABLE_TOP, TABLE_BOTTOM, include_uv=True)
    draw_sun_bar(draw, f, now, SUN_BAR_TOP)

    # Snap the anti-aliased text and bars so the panel sees six colors.
    return to_six_colors(canvas)


def draw_header(draw: ImageDraw.ImageDraw, f: Forecast, *, uv_peak: bool) -> None:
    """The big temperature, the description, and the high/low line.

    With ``uv_peak`` the day's peak UV is folded onto the high/low line (for the
    split screen, which has no room for the UV table row), drawn red when high.
    """
    draw.text(
        (MARGIN, TEMP_TOP),
        f"{f.now_temp}°",
        font=font("black", TEMP_SIZE),
        fill=PURE["black"],
        anchor="la",
    )
    draw.text(
        (HEADER_TEXT_X, DESC_TOP),
        f.description,
        font=font("bold", DESC_SIZE),
        fill=PURE["black"],
        anchor="la",
    )

    hilo_font = font("regular", HILO_SIZE)
    hilo = f"H {f.high}°    L {f.low}°"
    draw.text(
        (HEADER_TEXT_X, HILO_TOP), hilo, font=hilo_font, fill=PURE["black"], anchor="la"
    )
    if uv_peak:
        prefix_width = draw.textlength(hilo + "    ", font=hilo_font)
        color = PURE["red"] if f.uv_peak >= UV_WARN else PURE["black"]
        draw.text(
            (HEADER_TEXT_X + prefix_width, HILO_TOP),
            f"UV {f.uv_peak} at {_hour_label(f.uv_peak_time)}",
            font=hilo_font,
            fill=color,
            anchor="la",
        )


def draw_wear(draw: ImageDraw.ImageDraw, f: Forecast, top: int) -> None:
    """The dressing line on a full-width yellow band."""
    draw.rectangle(
        [0, top, WIDTH, top + WEAR_HEIGHT], fill=PURE["yellow"]
    )
    wear_font = font("bold", WEAR_SIZE)
    draw.text(
        (MARGIN, top + WEAR_HEIGHT // 2),
        what_to_wear(f),
        font=wear_font,
        fill=PURE["black"],
        anchor="lm",
    )


def draw_table(
    draw: ImageDraw.ImageDraw,
    f: Forecast,
    top: int,
    bottom: int,
    *,
    include_uv: bool,
) -> None:
    """The hourly table: an hour header row, then TEMP, RAIN, and optional UV.

    Each UV cell at or above the warning level is drawn white on a red block.
    """
    rows = [("TEMP", _temp_cells), ("RAIN", _rain_cells)]
    if include_uv:
        rows.append(("UV", _uv_cells))
    row_count = len(rows) + 1  # the hour header counts as a row
    row_height = (bottom - top) // row_count

    columns = len(f.hours)
    hour_area = WIDTH - MARGIN - (MARGIN + LABEL_COL_WIDTH)
    col_width = hour_area // columns if columns else hour_area
    col_x0 = MARGIN + LABEL_COL_WIDTH

    def center_x(index: int) -> int:
        return col_x0 + col_width * index + col_width // 2

    header_font = font("bold", ROW_LABEL_SIZE)
    header_cy = top + row_height // 2
    for index, hour in enumerate(f.hours):
        draw.text(
            (center_x(index), header_cy),
            _hour_label(hour.time),
            font=header_font,
            fill=PURE["black"],
            anchor="mm",
        )

    label_font = font("bold", ROW_LABEL_SIZE)
    cell_font = font("regular", CELL_SIZE)
    for row_index, (label, cells) in enumerate(rows, start=1):
        cy = top + row_height * row_index + row_height // 2
        draw.text(
            (MARGIN, cy), label, font=label_font, fill=PURE["black"], anchor="lm"
        )
        for index, (text, warn) in enumerate(cells(f)):
            cx = center_x(index)
            if warn:
                half = col_width // 2 - 6
                pad = row_height // 2 - 4
                draw.rectangle(
                    [cx - half, cy - pad, cx + half, cy + pad], fill=PURE["red"]
                )
                fill = PURE["white"]
            else:
                fill = PURE["black"]
            draw.text((cx, cy), text, font=cell_font, fill=fill, anchor="mm")


def draw_sun_bar(
    draw: ImageDraw.ImageDraw, f: Forecast, now: datetime, top: int
) -> None:
    """A sunrise-to-sunset bar filled by the fraction of daylight passed."""
    left = MARGIN
    right = WIDTH - MARGIN
    bottom = top + SUN_BAR_HEIGHT

    fraction = _daylight_fraction(f.sunrise, f.sunset, now)
    fill_right = left + round((right - left) * fraction)

    # Sunrise and sunset times sit above the bar, at its ends.
    label_font = font("regular", SUN_LABEL_SIZE)
    draw.text(
        (left, SUN_LABEL_TOP),
        _clock(f.sunrise),
        font=label_font,
        fill=PURE["black"],
        anchor="la",
    )
    draw.text(
        (right, SUN_LABEL_TOP),
        _clock(f.sunset),
        font=label_font,
        fill=PURE["black"],
        anchor="ra",
    )

    # Track in white, the passed daylight in yellow, a black outline around both.
    draw.rectangle([left, top, right, bottom], fill=PURE["white"])
    if fill_right > left:
        draw.rectangle([left, top, fill_right, bottom], fill=PURE["yellow"])
    draw.rectangle([left, top, right, bottom], outline=PURE["black"], width=2)


def _temp_cells(f: Forecast) -> list[tuple[str, bool]]:
    return [(f"{hour.temp}°", False) for hour in f.hours]


def _rain_cells(f: Forecast) -> list[tuple[str, bool]]:
    return [(f"{hour.rain_pct}%", False) for hour in f.hours]


def _uv_cells(f: Forecast) -> list[tuple[str, bool]]:
    return [(str(hour.uv), hour.uv >= UV_WARN) for hour in f.hours]


def _daylight_fraction(sunrise: datetime, sunset: datetime, now: datetime) -> float:
    """How far ``now`` sits between sunrise and sunset, clamped to ``[0, 1]``."""
    span = (sunset - sunrise).total_seconds()
    if span <= 0:
        return 0.0
    elapsed = (now - sunrise).total_seconds()
    return max(0.0, min(1.0, elapsed / span))


def _hour_label(time: datetime) -> str:
    """``7am``, ``1pm``, ``12pm`` from a datetime's hour."""
    suffix = "am" if time.hour < 12 else "pm"
    hour12 = time.hour % 12 or 12
    return f"{hour12}{suffix}"


def _clock(time: datetime) -> str:
    """``6:20am``, ``7:40pm`` from a datetime."""
    suffix = "am" if time.hour < 12 else "pm"
    hour12 = time.hour % 12 or 12
    return f"{hour12}:{time.minute:02d}{suffix}"
