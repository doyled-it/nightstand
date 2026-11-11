"""Now-playing state from the hub's ``now-playing.json``.

The hub's ``snaphouse-nowplaying`` service is the single source of now-playing
data for the house: it follows the House stream and writes ``now-playing.json``
into its public web root, served same-origin by nginx. The frame reads that one
file instead of talking to snapserver, so artwork lookups and position anchoring
all live on the hub.

``fetch_now_playing`` GETs the file and ``parse_now_playing`` reduces it to the
one thing the screen needs: the current track and the rooms it is playing in.
A missing, empty, or malformed file means nothing playing: ``fetch`` raises
(the caller's guard degrades it to ``None``) and ``parse`` returns ``None`` for
anything that is not a real track in an unmuted room.
"""

from __future__ import annotations

from datetime import datetime

import httpx

from nightstand.models import NowPlaying

_TIMEOUT = 5.0


def fetch_now_playing(client: httpx.Client, host: str) -> dict:
    """GET ``http://{host}/now-playing.json`` and return the parsed JSON.

    ``host`` is the hub's HTTP authority (host, or host:port off the default
    port 80). Raises on a non-200 response or a body that is not JSON, which the
    caller treats as nothing playing rather than crashing the loop.
    """
    response = client.get(f"http://{host}/now-playing.json", timeout=_TIMEOUT)
    response.raise_for_status()
    return response.json()


def parse_now_playing(doc: dict) -> NowPlaying | None:
    """Reduce a ``now-playing.json`` document to the current track, or ``None``.

    Returns ``None`` when nothing is playing: no track, a track with no title,
    or an empty ``rooms`` list (every room muted or disconnected, which the frame
    treats as nothing playing even though the hub still publishes it for the web
    page). Garbage in an optional field drops that field to ``None`` rather than
    discarding the whole track.
    """
    track = doc.get("track")
    if not isinstance(track, dict):
        return None

    title = track.get("title")
    if not title:
        return None

    rooms = tuple(r for r in doc.get("rooms") or () if isinstance(r, str))
    if not rooms:
        return None

    return NowPlaying(
        title=title,
        artist=_text(track.get("artist")),
        album=track.get("album") or None,
        art_url=track.get("art_url") or None,
        rooms=rooms,
        kind=track.get("kind") or "music",
        genre=tuple(g for g in track.get("genre") or () if isinstance(g, str)),
        status=track.get("status") or "playing",
        duration=_number(track.get("duration")),
        position=_number(track.get("position")),
        position_at=_datetime(track.get("position_at")),
        started_at=_datetime(track.get("started_at")),
    )


def _text(value: object) -> str:
    """A display string for an optional text field; ``None`` becomes empty."""
    return value if isinstance(value, str) else ""


def _number(value: object) -> float | None:
    """A float for a numeric field, or ``None`` when absent or not a number."""
    if isinstance(value, bool):  # bool is an int subclass; never a duration
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _datetime(value: object) -> datetime | None:
    """A ``datetime`` from an ISO-8601 string, or ``None`` when unparseable."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
