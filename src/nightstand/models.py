"""Shared data models.

``ArtPiece`` is the one piece of art the art screen draws; ``NowPlaying`` is the
track and rooms the now-playing screen draws. Other screens add their own models
as later tasks land.
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


@dataclass(frozen=True)
class NowPlaying:
    """The current track and where it is playing.

    ``album`` and ``art_url`` are ``None`` when the sender did not supply them
    (Bluetooth, for example, sends no cover art). ``rooms`` names the unmuted
    rooms the track is playing in, in the order the hub reports them.
    """

    title: str
    artist: str
    album: str | None = None
    art_url: str | None = None
    rooms: tuple[str, ...] = ()
