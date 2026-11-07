"""Shared constants and helpers for the screen renderers.

Every screen draws at the panel's native resolution. Keeping the size in one
place means a panel change is a single edit, and the screens stay in step.
``cover_crop`` scales an image to cover a target box and center-crops it (the
now-playing cover square uses it). ``contain_fit`` instead scales an image to
sit entirely inside a target box, preserving aspect, which is how the art
screen shows a whole piece without cropping.
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


def contain_fit(image: Image.Image, width: int, height: int) -> Image.Image:
    """Scale ``image`` to sit entirely inside ``width`` x ``height``.

    Preserves aspect ratio and never upscales past the box: the limiting side
    meets its bound and the other stays within it, so the whole image is
    visible with no cropping. The caller centers the result on its canvas.
    """
    source_width, source_height = image.size
    scale = min(width / source_width, height / source_height)
    return image.resize(
        (max(1, round(source_width * scale)), max(1, round(source_height * scale))),
        Image.Resampling.LANCZOS,
    )
