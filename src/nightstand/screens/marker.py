"""The system-health marker overlaid on whatever screen is showing.

When any machine is unhealthy, a small red square is drawn 8 px in from the
top-right corner. It is sized to notice up close, not to read from bed. A stale
data source is not a machine health problem and draws no marker.

The marker comes and goes with the next redraw that happens anyway; the app
folds its state into the redraw key, so it never forces a redraw on its own.
"""

from __future__ import annotations

from PIL import Image, ImageDraw

from nightstand.models import Health
from nightstand.palette import PURE
from nightstand.screens.common import WIDTH

SQUARE_SIZE = 14
INSET = 8


def apply(img: Image.Image, health: Health) -> Image.Image:
    """Return ``img`` with the health marker drawn, or ``img`` itself if clear."""
    if not health.problems:
        return img

    canvas = img.convert("RGB")
    draw = ImageDraw.Draw(canvas)

    right = WIDTH - INSET - 1
    left = right - (SQUARE_SIZE - 1)
    top = INSET
    bottom = top + (SQUARE_SIZE - 1)
    draw.rectangle([left, top, right, bottom], fill=PURE["red"])

    # The square is already a pure panel color, so no snap is needed.
    return canvas
