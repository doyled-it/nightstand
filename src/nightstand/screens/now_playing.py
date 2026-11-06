"""The now-playing screen: cover art and the track, with a row of room chips.

With cover art, a 480x480 dithered square sits on the left and the text column
runs down the right. Without art, the text column uses most of the width. The
title is largest, then artist, then album; a row of chips along the bottom names
the rooms the track is playing in.

Text and chips are drawn anti-aliased, then the whole canvas is snapped to the
six pure panel colors, so the panel only ever receives six colors. The cover is
Floyd-Steinberg dithered against the panel, which already lands on those colors.
"""

from __future__ import annotations

from PIL import Image, ImageDraw

from nightstand.models import NowPlaying
from nightstand.palette import PURE, dither_to_panel, to_six_colors
from nightstand.screens.common import HEIGHT, WIDTH
from nightstand.text import font, wrap

COVER_SIZE = 480

# Where the text column starts, and the right edge nothing it draws crosses.
TEXT_X_WITH_COVER = 506
TEXT_X_NO_COVER = 40
TEXT_RIGHT = 788
TEXT_TOP = 36

TITLE_SIZE = 64
TITLE_LINES = 3
ARTIST_SIZE = 38
ARTIST_LINES = 2
ARTIST_GAP = 12
ALBUM_SIZE = 28
ALBUM_GAP = 8

CHIP_SIZE = 22
CHIP_PAD_X = 12
CHIP_PAD_Y = 5
CHIP_GAP = 6
CHIP_BOTTOM = 446


def render(np: NowPlaying, cover: Image.Image | None) -> Image.Image:
    """Render the now-playing screen at 800x480."""
    canvas = Image.new("RGB", (WIDTH, HEIGHT), PURE["white"])
    draw = ImageDraw.Draw(canvas)

    if cover is not None:
        square = _square_crop(cover.convert("RGB"), COVER_SIZE)
        canvas.paste(dither_to_panel(square), (0, 0))
        text_x = TEXT_X_WITH_COVER
    else:
        text_x = TEXT_X_NO_COVER
    max_width = TEXT_RIGHT - text_x

    title_font = font("black", TITLE_SIZE)
    y = _draw_block(
        draw, np.title, title_font, text_x, TEXT_TOP, max_width, TITLE_LINES
    )
    if np.artist:
        artist_font = font("bold", ARTIST_SIZE)
        y = _draw_block(
            draw,
            np.artist,
            artist_font,
            text_x,
            y + ARTIST_GAP,
            max_width,
            ARTIST_LINES,
        )
    if np.album:
        album_font = font("regular", ALBUM_SIZE)
        _draw_block(draw, np.album, album_font, text_x, y + ALBUM_GAP, max_width, 1)

    _draw_chips(draw, np.rooms, text_x, max_width)

    # Snap the anti-aliased text and chip edges so the panel only sees six colors.
    return to_six_colors(canvas)


def _draw_block(
    draw: ImageDraw.ImageDraw,
    text: str,
    fnt,
    x: int,
    y: int,
    max_width: int,
    max_lines: int,
) -> int:
    """Draw ``text`` wrapped to ``max_lines`` and return the y below the block."""
    lines = wrap(draw, text, fnt, max_width, max_lines)
    ascent, descent = fnt.getmetrics()
    line_height = ascent + descent
    for line in lines:
        draw.text((x, y), line, font=fnt, fill=PURE["black"], anchor="la")
        y += line_height
    return y


def _draw_chips(
    draw: ImageDraw.ImageDraw,
    rooms: tuple[str, ...],
    x: int,
    max_width: int,
) -> None:
    """Draw room chips left to right, bottom-aligned, within the text column."""
    fnt = font("bold", CHIP_SIZE)
    ascent, descent = fnt.getmetrics()
    chip_height = ascent + descent + 2 * CHIP_PAD_Y
    top = CHIP_BOTTOM - chip_height
    right_limit = x + max_width

    cx = x
    for room in rooms:
        text_width = draw.textlength(room, font=fnt)
        chip_width = text_width + 2 * CHIP_PAD_X
        if cx > x and cx + chip_width > right_limit:
            break
        draw.rectangle([cx, top, cx + chip_width, CHIP_BOTTOM], fill=PURE["black"])
        draw.text(
            (cx + CHIP_PAD_X, top + CHIP_PAD_Y),
            room,
            font=fnt,
            fill=PURE["white"],
            anchor="la",
        )
        cx += chip_width + CHIP_GAP


def _square_crop(image: Image.Image, size: int) -> Image.Image:
    source_width, source_height = image.size
    scale = max(size / source_width, size / source_height)
    scaled = image.resize(
        (round(source_width * scale), round(source_height * scale)),
        Image.Resampling.LANCZOS,
    )
    scaled_width, scaled_height = scaled.size
    left = (scaled_width - size) // 2
    top = (scaled_height - size) // 2
    return scaled.crop((left, top, left + size, top + size))
