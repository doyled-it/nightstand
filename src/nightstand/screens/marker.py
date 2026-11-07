"""The system-health marker overlaid on whatever screen is showing.

When any machine is unhealthy or a data source this display needs has gone
quiet, a small red square is drawn 8 px in from the top-right corner. It is
sized to notice up close, not to read from bed. When a source outage set
``stale_since``, the time it began is drawn beside the square as ``since HH:MM``
so a glance tells you how long the display has been coasting on old data.

The marker comes and goes with the next redraw that happens anyway; the app
folds its state into the redraw key, so it never forces a redraw on its own.
"""

from __future__ import annotations

from PIL import Image, ImageDraw

from nightstand.models import Health
from nightstand.palette import PURE, to_six_colors
from nightstand.screens.common import WIDTH
from nightstand.text import font

SQUARE_SIZE = 14
INSET = 8
TEXT_SIZE = 16
TEXT_GAP = 6


def apply(img: Image.Image, health: Health) -> Image.Image:
    """Return ``img`` with the health marker drawn, or ``img`` itself if clear."""
    if not health.problems and health.stale_since is None:
        return img

    canvas = img.convert("RGB")
    draw = ImageDraw.Draw(canvas)

    right = WIDTH - INSET - 1
    left = right - (SQUARE_SIZE - 1)
    top = INSET
    bottom = top + (SQUARE_SIZE - 1)
    draw.rectangle([left, top, right, bottom], fill=PURE["red"])

    if health.stale_since is not None:
        text = f"since {health.stale_since:%H:%M}"
        draw.text(
            (left - TEXT_GAP, top + SQUARE_SIZE // 2),
            text,
            font=font("regular", TEXT_SIZE),
            fill=PURE["black"],
            anchor="rm",
        )

    # The square is pure, but the text's anti-aliased edges are not; snap the
    # whole canvas so the panel only ever receives six colors.
    return to_six_colors(canvas)
