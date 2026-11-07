"""Where a rendered screen goes: a file on the Mac, or the panel on the Pi.

``FileDisplay`` writes the image to a PNG, the main way previews are built and
tuned without hardware. With ``panel=True`` it first maps the pure colors to
their muted panel look, so the preview reads like the reflective e-ink will.
``InkyDisplay`` drives the real panel through Pimoroni's ``inky`` library; it
imports ``inky`` lazily so that importing nightstand on a Mac, where the
library is never installed, never fails.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from PIL import Image

from nightstand.palette import simulate_panel


class Display(Protocol):
    def show(self, img: Image.Image) -> None: ...


class FileDisplay:
    """Save the image to ``path`` as a PNG."""

    def __init__(self, path: Path, panel: bool = False) -> None:
        self.path = Path(path)
        self.panel = panel

    def show(self, img: Image.Image) -> None:
        out = simulate_panel(img) if self.panel else img
        out.save(self.path)


class InkyDisplay:
    """Push to the Inky Impression panel on the Pi.

    The image is already reduced to the six pure panel colors, so the library
    does no extra mapping: ``set_image`` then ``show`` paints it as given.

    ``saturation`` controls how vivid the six Spectra 6 inks render. Left unset,
    the library's own default stands; a value (0.0 to 1.0) is passed through for
    tuning against the real panel.
    """

    def __init__(self, saturation: float | None = None) -> None:
        # Imported here, not at module load, so a Mac without the Pi-only
        # ``inky`` extra can still import nightstand for previews and tests.
        from inky.auto import auto

        self._inky = auto()
        self._saturation = saturation

    def show(self, img: Image.Image) -> None:
        if self._saturation is None:
            self._inky.set_image(img)
        else:
            self._inky.set_image(img, saturation=self._saturation)
        self._inky.show()
