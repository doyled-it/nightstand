"""Where a rendered screen goes: a file on the Mac, or the panel on the Pi.

``FileDisplay`` writes the image to a PNG, the main way previews are built and
tuned without hardware. With ``panel=True`` it first maps the pure colors to
their muted panel look, so the preview reads like the reflective e-ink will.
The real Pi driver (``InkyDisplay``) arrives with panel bring-up.
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
