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

    ``id`` is the piece's path relative to the art directory (``met-334896.jpg``
    or ``Photos/beach.jpg``), the stable identity the hub and the rotation state
    use. It is empty only for a piece built outside the library (a preview
    stand-in), which never takes part in rotation.
    """

    path: Path
    title: str
    artist: str | None = None
    source_url: str | None = None
    id: str = ""


@dataclass(frozen=True)
class NowPlaying:
    """The current track and where it is playing.

    Filled from the hub's ``now-playing.json``. ``artist`` is an empty string
    when the sender gave none (internet radio, some apps); ``album`` and
    ``art_url`` are ``None`` when absent. ``rooms`` names the unmuted rooms the
    track is playing in, in the order the hub reports them.

    ``kind`` is ``"music"`` or ``"long_form"`` (podcasts, audiobooks, long
    mixes). ``genre`` is the hub's additive genre list, empty when unknown; the
    frame reads it only to derive the now-playing category for the kinds filter
    (see ``rules.category``). ``status`` is ``"playing"`` or ``"paused"``.
    ``duration`` and ``position`` are seconds. ``position`` is the playback
    position at the moment ``position_at`` was recorded, so a consumer
    extrapolates forward from that anchor while playing (see ``position_now``).
    ``started_at`` is when the hub first saw this track playing.
    """

    title: str
    artist: str
    album: str | None = None
    art_url: str | None = None
    rooms: tuple[str, ...] = ()
    kind: str = "music"
    genre: tuple[str, ...] = ()
    status: str = "playing"
    duration: float | None = None
    position: float | None = None
    position_at: datetime | None = None
    started_at: datetime | None = None

    def position_now(self, now: datetime) -> float | None:
        """The playback position at ``now``, or ``None`` without a position.

        While playing, it extrapolates forward from the ``(position,
        position_at)`` anchor; while paused it holds the recorded position. The
        result is floored at zero and, when a duration is known, clamped to it,
        so a resend or an over-run never carries the position past the track.
        """
        if self.position is None:
            return None
        pos = self.position
        if self.status == "playing" and self.position_at is not None:
            pos += (now - self.position_at).total_seconds()
        if pos < 0:
            pos = 0.0
        if self.duration is not None and pos > self.duration:
            pos = self.duration
        return pos


@dataclass(frozen=True)
class Health:
    """The system-health state the corner marker reflects.

    ``problems`` names each machine (this app included) that is on a fallback
    version or failing its checks, drawn from the published status files. The
    marker shows whenever it is non-empty. A stale data source is not a machine
    health problem and draws no marker.
    """

    problems: tuple[str, ...] = ()
