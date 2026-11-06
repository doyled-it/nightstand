"""The Spectra 6 color palette and conversions for the Inky Impression 7.3 inch.

The panel shows six colors only. ``PURE`` is the ideal RGB we draw and send;
``PANEL`` is how those colors actually read on the reflective e-ink (muted),
used only to simulate the look in Mac previews. Final values get tuned against
the real panel.

- ``to_six_colors`` snaps each pixel to the nearest pure color with no dither.
  Use it on rendered UI (text, bars, logos) where dithering would look dirty.
- ``dither_to_panel`` Floyd-Steinberg dithers against the muted panel colors for
  the best photographic result, then expresses the output in pure colors. Use it
  on art and album covers.
- ``simulate_panel`` swaps pure colors for their muted panel counterparts so a
  preview on a bright screen looks like the paper-white panel will.
"""

from __future__ import annotations

from PIL import Image

RGB = tuple[int, int, int]

PURE: dict[str, RGB] = {
    "black": (0, 0, 0),
    "white": (255, 255, 255),
    "red": (255, 0, 0),
    "yellow": (255, 255, 0),
    "green": (0, 255, 0),
    "blue": (0, 0, 255),
}

# Approximate how each pure color reads on the reflective Spectra 6 panel.
# Preview-only; retune on the real hardware.
PANEL: dict[str, RGB] = {
    "black": (43, 41, 42),
    "white": (233, 231, 224),
    "red": (173, 68, 60),
    "yellow": (206, 188, 88),
    "green": (74, 110, 75),
    "blue": (60, 70, 120),
}


def _flat(colors: list[RGB]) -> list[int]:
    """A flat 256-entry RGB palette list, padded by repeating the first color."""
    flat: list[int] = []
    for rgb in colors:
        flat.extend(rgb)
    first = colors[0]
    flat.extend(list(first) * (256 - len(colors)))
    return flat


def _palette_image(colors: list[RGB]) -> Image.Image:
    """A P-mode image whose palette is ``colors``, padded by repeating the first."""
    palette = Image.new("P", (1, 1))
    palette.putpalette(_flat(colors))
    return palette


def to_six_colors(img: Image.Image) -> Image.Image:
    """Snap each pixel to the nearest pure color, no dithering."""
    target = _palette_image(list(PURE.values()))
    quantized = img.convert("RGB").quantize(palette=target, dither=Image.Dither.NONE)
    return quantized.convert("RGB")


def dither_to_panel(img: Image.Image) -> Image.Image:
    """Floyd-Steinberg dither against the panel colors, output in pure colors."""
    target = _palette_image(list(PANEL.values()))
    quantized = img.convert("RGB").quantize(
        palette=target, dither=Image.Dither.FLOYDSTEINBERG
    )
    # Indices line up with PANEL order; re-point them at the pure colors.
    quantized.putpalette(_flat(list(PURE.values())))
    return quantized.convert("RGB")


def simulate_panel(img: Image.Image) -> Image.Image:
    """Map a pure-color image to its muted panel look for previews."""
    target = _palette_image(list(PURE.values()))
    quantized = img.convert("RGB").quantize(palette=target, dither=Image.Dither.NONE)
    quantized.putpalette(_flat(list(PANEL.values())))
    return quantized.convert("RGB")
