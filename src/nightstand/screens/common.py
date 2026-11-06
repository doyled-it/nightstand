"""Shared constants and helpers for the screen renderers.

Every screen draws at the panel's native resolution. Keeping the size in one
place means a panel change is a single edit, and the screens stay in step.
``cover_crop`` is the one scale-to-cover crop both the art and now-playing
screens use to fit an image to a target box without distorting it.
"""

from __future__ import annotations

from PIL import Image

WIDTH = 800
HEIGHT = 480


def cover_crop(image: Image.Image, width: int, height: int) -> Image.Image:
    """Scale ``image`` to cover ``width`` x ``height`` and center-crop to it."""
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
