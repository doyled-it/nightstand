"""Shared constants and helpers for the screen renderers.

Every screen draws at the panel's native resolution. Keeping the size in one
place means a panel change is a single edit, and the screens stay in step.
``cover_crop`` scales an image to cover a target box and center-crops it: the
now-playing and split cover squares use it. ``detail_crop`` covers the box the
same way but slides the crop window along the overflowing axis toward the
busiest part of the image, so the art screen does not cut through a subject.
"""

from __future__ import annotations

from PIL import Image, ImageFilter

WIDTH = 800
HEIGHT = 480

# When the source already matches the target aspect this closely (as an absolute
# difference of width/height ratios), the overflow is small enough that sliding
# the crop buys nothing, so detail_crop centers like cover_crop.
_ASPECT_TOLERANCE = 0.1

# The detail analysis runs on a downscaled grayscale copy with this many pixels
# on its short side. Small enough to stay fast on the Pi, large enough to tell
# a busy region from an empty one.
_ANALYSIS_SHORT_SIDE = 160


def cover_crop(image: Image.Image, width: int, height: int) -> Image.Image:
    """Scale ``image`` to cover ``width`` x ``height`` and center-crop to it."""
    scaled = _scale_to_cover(image, width, height)
    scaled_width, scaled_height = scaled.size
    left = (scaled_width - width) // 2
    top = (scaled_height - height) // 2
    return scaled.crop((left, top, left + width, top + height))


def detail_crop(image: Image.Image, width: int, height: int) -> Image.Image:
    """Cover ``width`` x ``height``, cropping toward the busiest region.

    The image is scaled to cover the box (so exactly one axis overflows), then
    the crop window slides along that axis to the position whose edge energy is
    highest, keeping a busy subject in frame rather than cutting through it. The
    scoring runs on a small grayscale copy and is deterministic; ties resolve to
    the centered window, so a uniform image crops exactly as ``cover_crop``.
    """
    scaled = _scale_to_cover(image, width, height)
    scaled_width, scaled_height = scaled.size
    horizontal_overflow = scaled_width - width
    vertical_overflow = scaled_height - height

    source_width, source_height = image.size
    source_aspect = source_width / source_height
    if abs(source_aspect - width / height) <= _ASPECT_TOLERANCE:
        left = horizontal_overflow // 2
        top = vertical_overflow // 2
        return scaled.crop((left, top, left + width, top + height))

    if horizontal_overflow >= vertical_overflow:
        energies = _axis_energy(scaled, axis=0)
        left = _best_offset(energies, width, scaled_width, horizontal_overflow)
        top = vertical_overflow // 2
    else:
        energies = _axis_energy(scaled, axis=1)
        top = _best_offset(energies, height, scaled_height, vertical_overflow)
        left = horizontal_overflow // 2
    return scaled.crop((left, top, left + width, top + height))


def _scale_to_cover(image: Image.Image, width: int, height: int) -> Image.Image:
    source_width, source_height = image.size
    scale = max(width / source_width, height / source_height)
    return image.resize(
        (round(source_width * scale), round(source_height * scale)),
        Image.Resampling.LANCZOS,
    )


def _axis_energy(scaled: Image.Image, axis: int) -> list[int]:
    """Edge energy per column (axis 0) or per row (axis 1) of ``scaled``.

    Works on a small grayscale copy so the cost does not grow with the real
    image. The one-pixel border is dropped before summing because the edge
    filter produces artefacts there that have nothing to do with the content.
    """
    scaled_width, scaled_height = scaled.size
    short = min(scaled_width, scaled_height)
    ratio = _ANALYSIS_SHORT_SIDE / short
    small = scaled.convert("L").resize(
        (max(round(scaled_width * ratio), 3), max(round(scaled_height * ratio), 3)),
        Image.Resampling.BOX,
    )
    edges = small.filter(ImageFilter.FIND_EDGES)
    interior = edges.crop((1, 1, edges.width - 1, edges.height - 1))
    if axis == 0:
        line = interior.resize((interior.width, 1), Image.Resampling.BOX)
    else:
        line = interior.resize((1, interior.height), Image.Resampling.BOX)
    # tobytes on an "L" image is one byte per pixel; it avoids the deprecated
    # getdata while giving the same per-bin energies.
    return list(line.tobytes())


def _best_offset(
    energies: list[int], window: int, scaled_span: int, overflow: int
) -> int:
    """The crop offset in ``scaled`` coordinates with the most edge energy.

    ``energies`` scores the overflowing axis on the small analysis copy. The
    window summing the most energy wins; ties go to the window whose center sits
    nearest the energy centroid, so the busiest region lands mid-frame and a
    uniform image stays centered. The chosen small-space offset is mapped back
    proportionally onto the real ``overflow``.
    """
    bins = len(energies)
    window_bins = max(1, min(bins, round(window / scaled_span * bins)))
    slide = bins - window_bins
    if slide <= 0:
        return overflow // 2

    prefix = [0]
    for value in energies:
        prefix.append(prefix[-1] + value)

    total = prefix[-1]
    if total > 0:
        centroid = sum(i * e for i, e in enumerate(energies)) / total
    else:
        centroid = (bins - 1) / 2

    best_k = 0
    best_score = -1
    best_gap = float("inf")
    for k in range(slide + 1):
        score = prefix[k + window_bins] - prefix[k]
        gap = abs(k + window_bins / 2 - centroid)
        if score > best_score or (score == best_score and gap < best_gap):
            best_score = score
            best_gap = gap
            best_k = k
    return round(best_k / slide * overflow)
