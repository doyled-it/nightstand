"""The art screen: the whole piece, scaled to fit, on a white border.

The image is scaled to sit entirely inside 800x480 with its aspect ratio
preserved, Floyd-Steinberg dithered to the panel's six colors, and centered on
a pure-white 800x480 canvas. The white margins fill whichever two sides the
image does not reach. There is no caption bar: the owner chose to show the art
alone and keep its history (with titles and museum links) off the panel.
"""

from __future__ import annotations

from PIL import Image

from nightstand.models import ArtPiece
from nightstand.palette import PURE, dither_to_panel
from nightstand.screens.common import HEIGHT, WIDTH, contain_fit


def render(piece: ArtPiece, image: Image.Image) -> Image.Image:
    """Render ``piece``'s ``image`` whole on an 800x480 white canvas."""
    # ``piece`` is unused: the panel shows the image alone, and the caption now
    # lives in the published history rather than on screen. The parameter stays
    # so every screen keeps the same ``render(model, image)`` shape.
    del piece
    fitted = contain_fit(image.convert("RGB"), WIDTH, HEIGHT)
    dithered = dither_to_panel(fitted)

    canvas = Image.new("RGB", (WIDTH, HEIGHT), PURE["white"])
    offset = ((WIDTH - fitted.width) // 2, (HEIGHT - fitted.height) // 2)
    canvas.paste(dithered, offset)
    # The dithered image is already six pure colors and the border is pure
    # white, so the canvas is panel-ready without a further snap.
    return canvas
