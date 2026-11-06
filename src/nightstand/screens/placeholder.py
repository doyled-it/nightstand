"""The placeholder screen: a centered message on a plain panel.

Shown when a screen has nothing to draw, for example when the art folder is
empty. Black text centered on white, wrapped to fit.
"""

from __future__ import annotations

from PIL import Image, ImageDraw

from nightstand.palette import PURE, to_six_colors
from nightstand.text import font, wrap

WIDTH = 800
HEIGHT = 480
MESSAGE_SIZE = 44
PADDING = 48
MAX_LINES = 3
LINE_SPACING = 14


def render(message: str) -> Image.Image:
    """Render ``message`` centered on an 800x480 white panel."""
    canvas = Image.new("RGB", (WIDTH, HEIGHT), PURE["white"])
    draw = ImageDraw.Draw(canvas)

    message_font = font("bold", MESSAGE_SIZE)
    lines = wrap(draw, message, message_font, WIDTH - 2 * PADDING, MAX_LINES)

    ascent, descent = message_font.getmetrics()
    line_height = ascent + descent
    block_height = line_height * len(lines) + LINE_SPACING * (len(lines) - 1)
    y = (HEIGHT - block_height) // 2

    for line in lines:
        draw.text(
            (WIDTH // 2, y),
            line,
            font=message_font,
            fill=PURE["black"],
            anchor="ma",
        )
        y += line_height + LINE_SPACING
    # Snap the anti-aliased text so the panel only ever receives six colors.
    return to_six_colors(canvas)
