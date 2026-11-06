"""The split screen: the morning weather above a now-playing strip.

It reuses the weather screen's header, dressing band, and hourly table, with two
changes that make room for audio: the UV table row is dropped and the day's peak
UV is folded onto the high/low line instead, and the sunrise-to-sunset bar is
replaced by an 84 px black strip along the bottom. The strip carries a small
cover square (a yellow block when there is none), the track title and artist, and
the rooms it is playing in, right-aligned.

Everything is drawn anti-aliased, then the whole canvas is snapped to the six
pure panel colors, so the panel only ever receives six colors.
"""

from __future__ import annotations

from datetime import datetime

from PIL import Image, ImageDraw

from nightstand.models import NowPlaying
from nightstand.palette import PURE, dither_to_panel, to_six_colors
from nightstand.screens.common import HEIGHT, WIDTH, cover_crop
from nightstand.screens.weather import (
    MARGIN,
    WEAR_TOP,
    draw_header,
    draw_table,
    draw_wear,
)
from nightstand.sources.weather import Forecast
from nightstand.text import font, wrap

TABLE_TOP = 266
TABLE_BOTTOM = 388

STRIP_TOP = 396
STRIP_HEIGHT = HEIGHT - STRIP_TOP

COVER_SIZE = 60
COVER_GAP = 16

TITLE_SIZE = 32
ARTIST_SIZE = 28
ROOMS_SIZE = 22

# The title and artist never shrink below this, however long the room list is;
# the rooms text is truncated with an ellipsis first to protect it.
MIN_TEXT_WIDTH = 240


def render(
    f: Forecast, np: NowPlaying, cover: Image.Image | None, now: datetime
) -> Image.Image:
    """Render the split screen for ``f`` and ``np`` as of ``now`` at 800x480."""
    canvas = Image.new("RGB", (WIDTH, HEIGHT), PURE["white"])
    draw = ImageDraw.Draw(canvas)

    draw_header(draw, f, uv_peak=True)
    draw_wear(draw, f, WEAR_TOP)
    draw_table(draw, f, TABLE_TOP, TABLE_BOTTOM, include_uv=False)
    _draw_strip(canvas, draw, np, cover)

    # ``now`` is unused for the split layout, but kept in the signature so both
    # screens render from the same (forecast, now) pair the scheduler holds.
    del now

    # Snap text, bars, and the cover-square edges so the panel sees six colors.
    return to_six_colors(canvas)


def _draw_strip(
    canvas: Image.Image,
    draw: ImageDraw.ImageDraw,
    np: NowPlaying,
    cover: Image.Image | None,
) -> None:
    draw.rectangle([0, STRIP_TOP, WIDTH, HEIGHT], fill=PURE["black"])

    cover_y = STRIP_TOP + (STRIP_HEIGHT - COVER_SIZE) // 2
    if cover is not None:
        square = cover_crop(cover.convert("RGB"), COVER_SIZE, COVER_SIZE)
        canvas.paste(dither_to_panel(square), (MARGIN, cover_y))
    else:
        draw.rectangle(
            [MARGIN, cover_y, MARGIN + COVER_SIZE, cover_y + COVER_SIZE],
            fill=PURE["yellow"],
        )

    text_x = MARGIN + COVER_SIZE + COVER_GAP
    rooms_font = font("bold", ROOMS_SIZE)

    # Give the title and artist a guaranteed minimum width, then let the rooms
    # take whatever is left, truncated with an ellipsis so a long room list never
    # starves the track text.
    max_rooms_width = WIDTH - MARGIN - text_x - MIN_TEXT_WIDTH - COVER_GAP
    rooms_text = " · ".join(np.rooms)
    if rooms_text and max_rooms_width > 0:
        rooms_text = wrap(draw, rooms_text, rooms_font, int(max_rooms_width), 1)[0]
    elif max_rooms_width <= 0:
        rooms_text = ""

    rooms_width = draw.textlength(rooms_text, font=rooms_font) if rooms_text else 0
    right_limit = WIDTH - MARGIN - (rooms_width + COVER_GAP if rooms_text else 0)
    max_width = max(MIN_TEXT_WIDTH, int(right_limit - text_x))

    title_font = font("bold", TITLE_SIZE)
    title = wrap(draw, np.title, title_font, max_width, 1)[0]
    draw.text(
        (text_x, STRIP_TOP + 14),
        title,
        font=title_font,
        fill=PURE["white"],
        anchor="la",
    )
    if np.artist:
        artist_font = font("regular", ARTIST_SIZE)
        artist = wrap(draw, np.artist, artist_font, max_width, 1)[0]
        draw.text(
            (text_x, STRIP_TOP + 50),
            artist,
            font=artist_font,
            fill=PURE["white"],
            anchor="la",
        )

    if rooms_text:
        draw.text(
            (WIDTH - MARGIN, STRIP_TOP + STRIP_HEIGHT // 2),
            rooms_text,
            font=rooms_font,
            fill=PURE["white"],
            anchor="rm",
        )
