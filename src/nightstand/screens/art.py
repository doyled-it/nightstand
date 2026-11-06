"""The art screen: a full-bleed dithered image with a caption bar.

The image is scaled to cover 800x480 and center-cropped, then Floyd-Steinberg
dithered to the panel's six colors. A black bar along the bottom carries the
caption in bold white: ``"{title} · {artist}"``, or just the title when
there is no artist.
"""

from __future__ import annotations

from PIL import Image, ImageDraw

from nightstand.models import ArtPiece
from nightstand.palette import PURE, dither_to_panel, to_six_colors
from nightstand.text import font, wrap

WIDTH = 800
HEIGHT = 480
CAPTION_HEIGHT = 44
CAPTION_PADDING = 16
CAPTION_SIZE = 24


def render(piece: ArtPiece, image: Image.Image) -> Image.Image:
    """Render ``piece`` over ``image`` at 800x480."""
    cropped = _cover_crop(image.convert("RGB"), WIDTH, HEIGHT)
    canvas = dither_to_panel(cropped)

    draw = ImageDraw.Draw(canvas)
    bar_top = HEIGHT - CAPTION_HEIGHT
    draw.rectangle([0, bar_top, WIDTH, HEIGHT], fill=PURE["black"])

    caption_font = font("bold", CAPTION_SIZE)
    max_width = WIDTH - 2 * CAPTION_PADDING
    lines = wrap(draw, _caption(piece), caption_font, max_width, 1)
    line = lines[0] if lines else ""
    draw.text(
        (CAPTION_PADDING, bar_top + CAPTION_HEIGHT // 2),
        line,
        font=caption_font,
        fill=PURE["white"],
        anchor="lm",
    )
    # The dithered art is already pure, but the caption's anti-aliased edges are
    # not. Snap the whole canvas so the panel only ever receives six colors.
    return to_six_colors(canvas)


def _caption(piece: ArtPiece) -> str:
    if piece.artist:
        return f"{piece.title} · {piece.artist}"
    return piece.title


def _cover_crop(image: Image.Image, width: int, height: int) -> Image.Image:
    source_width, source_height = image.size
    scale = max(width / source_width, height / source_height)
    scaled = image.resize(
        (round(source_width * scale), round(source_height * scale)),
        Image.Resampling.LANCZOS,
    )
    scaled_width, scaled_height = scaled.size
    left = (scaled_width - width) // 2
    top = (scaled_height - height) // 2
    return scaled.crop((left, top, left + width, top + height))
