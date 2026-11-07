"""Shared data models.

``ArtPiece`` is the one piece of art the art screen draws; ``NowPlaying`` is the
track and rooms the now-playing screen draws. Other screens add their own models
as later tasks land.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True)
class ArtPiece:
    """One image in the art library, with its caption.

    ``artist`` is ``None`` when the caption has only a title. ``source_url`` is
    the museum object page from the sidecar's optional third line, or ``None``
    when the sidecar has no third line or it is not an https URL.
    """

    path: Path
    title: str
    artist: str | None = None
    source_url: str | None = None


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


@dataclass(frozen=True)
class Health:
    """The system-health state the corner marker reflects.

    ``problems`` names each machine (this app included) that is on a fallback
    version or failing its checks, drawn from the published status files.
    ``stale_since`` is when the current data-source outage began, or ``None``
    when every source this display depends on is answering. The marker shows
    whenever either is set.
    """

    problems: tuple[str, ...] = ()
    stale_since: datetime | None = None
