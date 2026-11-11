"""The now-playing screen: cover, title, artist, album, a progress strip, chips.

Layout "B", chosen by the owner for both music and long-form audio. A 320x320
cover sits in the top-left corner. Beside it (or from the left margin when there
is no cover) runs the text column: the title in Inter Black, up to four lines,
shrinking from 52 px down to 28 px until it fits and only ellipsized at 28 px;
then the artist in bold and the album in regular, each one line. A full-width
strip below the cover holds the progress line, and the room chips sit at the
bottom-left.

Progress follows the track's kind. A long-form track (podcast, audiobook, long
mix) with a known position draws a bar and ``23:10 of 52:00  ·  ends 9:46``.
Music with a known duration draws just ``5:22  ·  ends 9:14``. A track with no
duration draws no progress line. The end time is ``now`` plus the time left, or
``started_at`` plus the duration when there is no position.

Text, bar, and chips are drawn anti-aliased, then the whole canvas is snapped to
the six pure panel colors. The cover is Floyd-Steinberg dithered against the
panel, which already lands on those colors.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from PIL import Image, ImageDraw, ImageFont

from nightstand.models import NowPlaying
from nightstand.palette import PURE, dither_to_panel, to_six_colors
from nightstand.screens.common import HEIGHT, WIDTH, cover_crop
from nightstand.text import clock_time, font, wrap

COVER_SIZE = 320

# A cover whose aspect is within this fraction of square keeps today's cover
# crop; one further off square than this is shown whole inside the square on a
# paper background, so a tall audiobook cover is never cut and loses no title.
COVER_SQUARE_TOLERANCE = 0.05

# The full-width strip and the right edge both keep this margin; the text column
# beside the cover starts just past the cover, and the column without a cover
# starts a little further in from the left edge.
MARGIN = 28
TEXT_X_WITH_COVER = 342
TEXT_X_NO_COVER = 40
TEXT_RIGHT = WIDTH - MARGIN
TEXT_TOP = 24

# The title shrinks across these sizes (largest first) until it wraps to at most
# TITLE_LINES without dropping any words; at the smallest size it is ellipsized.
TITLE_SIZE_MAX = 52
TITLE_SIZE_MIN = 28
TITLE_SIZE_STEP = 2
TITLE_LINES = 4
_ELLIPSIS = "…"

ARTIST_SIZE = 30
ARTIST_GAP = 10
ALBUM_SIZE = 26
ALBUM_GAP = 8

# The bottom band, below the cover. The bar (long-form only) sits at the top of
# the strip, the progress line under it, and the chips along the bottom. STRIP_TOP
# is the ceiling the text column must clear; the worst-case title block (four
# lines at 52 px plus an artist and album line) ends a few pixels above it.
STRIP_TOP = 376
BAR_HEIGHT = 14
PROGRESS_TOP = 396
PROGRESS_SIZE = 26
# The separator between the duration and the end time, spaced as the owner's
# mockup showed it: ``23:10 of 52:00  ·  ends 9:46``.
_SEP = "  ·  "
# At or past an hour the duration and position read as h:mm:ss, matching the
# approved mockup and the room-picker card; below it they read as m:ss.
_HOUR = 3600

CHIP_SIZE = 24
CHIP_PAD_X = 12
CHIP_PAD_Y = 5
CHIP_GAP = 6
CHIP_BOTTOM = 475


def render(np: NowPlaying, cover: Image.Image | None, now: datetime) -> Image.Image:
    """Render the now-playing screen for ``np`` as of ``now`` at 800x480."""
    canvas = Image.new("RGB", (WIDTH, HEIGHT), PURE["white"])
    draw = ImageDraw.Draw(canvas)

    if cover is not None:
        canvas.paste(_fit_cover(cover, COVER_SIZE), (0, 0))
        text_x = TEXT_X_WITH_COVER
    else:
        text_x = TEXT_X_NO_COVER
    max_width = TEXT_RIGHT - text_x

    title_font, title_lines = fit_title(draw, np.title, max_width)
    y = _draw_lines(draw, title_lines, title_font, text_x, TEXT_TOP)
    if np.artist:
        artist_font = font("bold", ARTIST_SIZE)
        y = _draw_block(
            draw, np.artist, artist_font, text_x, y + ARTIST_GAP, max_width
        )
    if np.album:
        album_font = font("regular", ALBUM_SIZE)
        _draw_block(draw, np.album, album_font, text_x, y + ALBUM_GAP, max_width)

    _draw_strip(draw, np, now)
    _draw_chips(draw, np.rooms)

    # Snap the anti-aliased text, bar, and chip edges to the six panel colors.
    return to_six_colors(canvas)


def _fit_cover(cover: Image.Image, size: int) -> Image.Image:
    """The cover as a ``size`` x ``size`` square ready to paste.

    A near-square cover (aspect within ``COVER_SQUARE_TOLERANCE`` of 1) keeps the
    cover crop, filling the square. A cover further off square is scaled to fit
    whole inside the square and centred on a paper (white) background, so nothing
    is cropped: a tall audiobook cover keeps its title and a wide one its edges.
    The cover itself is Floyd-Steinberg dithered; the margins stay pure paper.
    """
    source = cover.convert("RGB")
    width, height = source.size
    if abs(width / height - 1.0) <= COVER_SQUARE_TOLERANCE:
        return dither_to_panel(cover_crop(source, size, size))
    scale = min(size / width, size / height)
    new_width = max(1, round(width * scale))
    new_height = max(1, round(height * scale))
    resized = source.resize((new_width, new_height), Image.Resampling.LANCZOS)
    square = Image.new("RGB", (size, size), PURE["white"])
    square.paste(
        dither_to_panel(resized), ((size - new_width) // 2, (size - new_height) // 2)
    )
    return square


def fit_title(
    draw: ImageDraw.ImageDraw, text: str, max_width: int
) -> tuple[ImageFont.FreeTypeFont, list[str]]:
    """The largest title font that wraps ``text`` to at most ``TITLE_LINES``.

    Steps down from ``TITLE_SIZE_MAX`` to ``TITLE_SIZE_MIN`` and returns the first
    size at which the title fits without any line being ellipsized. If it never
    fits (an enormous title, or a script Inter cannot break on spaces), it is
    drawn at the smallest size with the last line ellipsized. Returns the font and
    the wrapped lines.
    """
    for size in range(TITLE_SIZE_MAX, TITLE_SIZE_MIN - 1, -TITLE_SIZE_STEP):
        fnt = font("black", size)
        lines = wrap(draw, text, fnt, max_width, TITLE_LINES)
        if not any(line.endswith(_ELLIPSIS) for line in lines):
            return fnt, lines
    fnt = font("black", TITLE_SIZE_MIN)
    return fnt, wrap(draw, text, fnt, max_width, TITLE_LINES)


def end_time(np: NowPlaying, now: datetime) -> datetime | None:
    """When the track ends, or ``None`` without a duration to reckon from.

    With a known position it is ``now`` plus the time left (the position
    extrapolated to ``now``); otherwise it is ``started_at`` plus the duration.
    With neither a position nor a start time there is nothing to show.
    """
    if np.duration is None:
        return None
    position = np.position_now(now)
    if position is not None:
        return now + timedelta(seconds=np.duration - position)
    if np.started_at is not None:
        return np.started_at + timedelta(seconds=np.duration)
    return None


def _draw_strip(draw: ImageDraw.ImageDraw, np: NowPlaying, now: datetime) -> None:
    """The progress bar (long-form only) and the duration/end-time line."""
    if np.duration is None:
        return
    position = np.position_now(now)
    if np.kind == "long_form" and position is not None:
        _draw_bar(draw, position / np.duration)

    draw.text(
        (MARGIN, PROGRESS_TOP),
        progress_text(np, now),
        font=font("regular", PROGRESS_SIZE),
        fill=PURE["black"],
        anchor="la",
    )


def progress_text(np: NowPlaying, now: datetime) -> str | None:
    """The strip's duration line, or ``None`` when there is no duration.

    Long-form with a known position reads ``elapsed of total``; everything else
    reads just the total. Both numbers use the same clock format, decided by the
    duration: ``h:mm:ss`` once the duration reaches an hour, ``m:ss`` below it,
    so a long podcast reads ``1:00:10 of 1:37:25``. The end time is appended when
    it can be reckoned.
    """
    if np.duration is None:
        return None
    hours = np.duration >= _HOUR
    position = np.position_now(now)
    if np.kind == "long_form" and position is not None:
        text = f"{_hms(position, hours=hours)} of {_hms(np.duration, hours=hours)}"
    else:
        text = _hms(np.duration, hours=hours)

    end = end_time(np, now)
    if end is not None:
        text += f"{_SEP}ends {clock_time(end)}"
    return text


def _draw_bar(draw: ImageDraw.ImageDraw, fraction: float) -> None:
    """A full-width elapsed bar: red fill to ``fraction``, a black 2 px outline."""
    left, right = MARGIN, TEXT_RIGHT
    bottom = STRIP_TOP + BAR_HEIGHT
    fill_right = left + round((right - left) * max(0.0, min(1.0, fraction)))
    if fill_right > left:
        draw.rectangle([left, STRIP_TOP, fill_right, bottom], fill=PURE["red"])
    draw.rectangle([left, STRIP_TOP, right, bottom], outline=PURE["black"], width=2)


def _hms(seconds: float, *, hours: bool) -> str:
    """Format seconds as a clock time: ``1:37:25`` with ``hours``, else ``23:10``.

    Hours are never zero-padded; minutes and seconds are. The caller chooses
    ``hours`` once for a whole line (by the total), so the elapsed and total read
    in the same format.
    """
    total = int(round(seconds))
    if hours:
        hrs, rem = divmod(total, _HOUR)
        minutes, secs = divmod(rem, 60)
        return f"{hrs}:{minutes:02d}:{secs:02d}"
    minutes, secs = divmod(total, 60)
    return f"{minutes}:{secs:02d}"


def _draw_lines(
    draw: ImageDraw.ImageDraw,
    lines: list[str],
    fnt,
    x: int,
    y: int,
) -> int:
    """Draw ``lines`` top-aligned from ``y`` and return the y below the block."""
    ascent, descent = fnt.getmetrics()
    line_height = ascent + descent
    for line in lines:
        draw.text((x, y), line, font=fnt, fill=PURE["black"], anchor="la")
        y += line_height
    return y


def _draw_block(
    draw: ImageDraw.ImageDraw,
    text: str,
    fnt,
    x: int,
    y: int,
    max_width: int,
) -> int:
    """Wrap ``text`` to a single line and draw it; return the y below it."""
    lines = wrap(draw, text, fnt, max_width, 1)
    return _draw_lines(draw, lines, fnt, x, y)


def _draw_chips(draw: ImageDraw.ImageDraw, rooms: tuple[str, ...]) -> None:
    """Draw room chips left to right along the bottom, from the left margin."""
    fnt = font("bold", CHIP_SIZE)
    ascent, descent = fnt.getmetrics()
    chip_height = ascent + descent + 2 * CHIP_PAD_Y
    top = CHIP_BOTTOM - chip_height

    cx = MARGIN
    for room in rooms:
        text_width = draw.textlength(room, font=fnt)
        chip_width = text_width + 2 * CHIP_PAD_X
        if cx > MARGIN and cx + chip_width > TEXT_RIGHT:
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
