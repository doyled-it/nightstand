"""Shared data models.

For now this holds only ``ArtPiece``, the one piece of art the art screen draws.
Other screens add their own models as later tasks land.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ArtPiece:
    """One image in the art library, with its caption.

    ``artist`` is ``None`` when the caption has only a title.
    """

    path: Path
    title: str
    artist: str | None = None
