"""The art screen: the whole panel filled with the piece.

The image is scaled to cover the 800x480 panel with its aspect ratio
preserved, the overflow is center-cropped away, and the result is
Floyd-Steinberg dithered to the panel's six colors. The art reaches every
edge, so there is no white border and no caption bar: the owner chose to show
the art alone and keep its history (with titles and museum links) off the
panel.
"""

from __future__ import annotations

from PIL import Image

from nightstand.models import ArtPiece
from nightstand.palette import dither_to_panel
from nightstand.screens.common import HEIGHT, WIDTH, detail_crop


def render(piece: ArtPiece, image: Image.Image) -> Image.Image:
    """Render ``piece``'s ``image`` filling the whole 800x480 panel."""
    # ``piece`` is unused: the panel shows the image alone, and the caption now
    # lives in the published history rather than on screen. The parameter stays
    # so every screen keeps the same ``render(model, image)`` shape.
    del piece
    # Detail-aware crop slides the window toward the busiest region so a subject
    # near an edge is not cut; a centered image still crops centered.
    filled = detail_crop(image.convert("RGB"), WIDTH, HEIGHT)
    # The dithered image is already the six pure panel colors, so the result is
    # panel-ready without a further snap.
    return dither_to_panel(filled)
