"""The art screen: the whole panel filled with the piece.

The image is scaled to cover the 800x480 panel with its aspect ratio
preserved, the overflow is center-cropped away, and the result is
Floyd-Steinberg dithered to the panel's six colors. The art reaches every
edge, so there is no white border and no caption bar: the owner chose to show
the art alone and keep its history (with titles and museum links) off the
panel.
"""

from __future__ import annotations

import logging

from PIL import Image

from nightstand.crops import as_dict, parse_box
from nightstand.models import ArtPiece
from nightstand.palette import dither_to_panel
from nightstand.screens.common import HEIGHT, WIDTH, crop_to_panel, detail_crop

logger = logging.getLogger(__name__)

# Pieces whose crop failed, so a persistent failure is logged once, not per draw.
_logged_failures: set[str] = set()


def render(piece: ArtPiece, image: Image.Image) -> Image.Image:
    """Render ``piece``'s ``image`` filling the whole 800x480 panel.

    ``image`` is the upright picture (``load_piece_image`` applies EXIF
    orientation). The piece's box (its saved ``crop``, else the hub's
    ``auto_crop``) is cut and resized; a missing or unusable box, or any failure
    cutting it, falls back to the detail-aware ``detail_crop``.
    """
    rgb = image.convert("RGB")
    filled = _cut_box(piece, rgb)
    if filled is None:
        # Detail-aware crop slides the window toward the busiest region so a
        # subject near an edge is not cut; a centered image crops centered.
        filled = detail_crop(rgb, WIDTH, HEIGHT)
    # The dithered image is already the six pure panel colors, so the result is
    # panel-ready without a further snap.
    return dither_to_panel(filled)


def _cut_box(piece: ArtPiece, image: Image.Image) -> Image.Image | None:
    """The piece's box cut to the panel, or ``None`` to use ``detail_crop``."""
    for candidate in (piece.crop, piece.auto_crop):
        box = parse_box(as_dict(candidate)) if candidate is not None else None
        if box is None:
            continue
        try:
            return crop_to_panel(image, box)
        except Exception as error:
            key = piece.id or str(piece.path)
            if key not in _logged_failures:
                _logged_failures.add(key)
                logger.warning("crop for %s failed, using detail crop: %s", key, error)
            return None
    return None
