"""Fonts and word wrapping for the rendered screens.

The Inter static TTFs ship inside the package (see ``fonts/OFL.txt``). ``font``
loads a weight at a pixel size; ``wrap`` breaks a string into at most
``max_lines`` that each fit ``max_width``, ending the last line with an ellipsis
when text had to be dropped.
"""

from __future__ import annotations

import io
from functools import cache
from importlib import resources
from typing import Literal

from PIL import ImageDraw, ImageFont

Weight = Literal["regular", "bold", "black"]

_FILES: dict[str, str] = {
    "regular": "Inter-Regular.ttf",
    "bold": "Inter-Bold.ttf",
    "black": "Inter-Black.ttf",
}

_ELLIPSIS = "…"


@cache
def _font_bytes(weight: Weight) -> bytes:
    return resources.files("nightstand.fonts").joinpath(_FILES[weight]).read_bytes()


def font(weight: Weight, size: int) -> ImageFont.FreeTypeFont:
    """Load an Inter weight at ``size`` pixels."""
    return ImageFont.truetype(io.BytesIO(_font_bytes(weight)), size)


def wrap(
    draw: ImageDraw.ImageDraw,
    s: str,
    font: ImageFont.FreeTypeFont,
    max_width: int,
    max_lines: int,
) -> list[str]:
    """Wrap ``s`` to at most ``max_lines`` lines that fit ``max_width`` pixels.

    The last line ends with an ellipsis when words had to be dropped.
    """
    words = s.split()
    lines: list[str] = []
    current: list[str] = []
    overflowed = False

    for word in words:
        trial = " ".join([*current, word])
        if current and draw.textlength(trial, font=font) > max_width:
            lines.append(" ".join(current))
            current = [word]
            if len(lines) == max_lines:
                overflowed = True
                break
        else:
            current.append(word)

    if not overflowed and current:
        lines.append(" ".join(current))

    if overflowed or len(lines) > max_lines:
        kept = lines[: max_lines - 1]
        kept.append(_ellipsize(draw, lines[max_lines - 1], font, max_width))
        return kept
    return lines


def _ellipsize(
    draw: ImageDraw.ImageDraw,
    line: str,
    font: ImageFont.FreeTypeFont,
    max_width: int,
) -> str:
    text = line
    while text and draw.textlength(text + _ELLIPSIS, font=font) > max_width:
        text = text[:-1].rstrip()
    return text + _ELLIPSIS
