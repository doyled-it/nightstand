"""Now-playing state from the hub's snapserver.

snapserver exposes a JSON-RPC API; ``Server.GetStatus`` returns every stream,
group, and client. ``fetch_status`` makes that call and ``parse_now_playing``
reduces the response to the one thing the screen needs: the track that is
actually audible somewhere, and which rooms it is playing in.

A room counts as playing when its group is assigned to the active stream, the
group is not muted, the client is connected, and the client is not muted. The
active stream is the first ``playing`` stream that at least one unmuted group is
assigned to. If nothing is playing to an unmuted room, or the active stream has
no title, there is nothing to show.
"""

from __future__ import annotations

import json

import httpx

from nightstand.models import NowPlaying

_TIMEOUT = 5.0

# ``Server.GetStatus`` carries base64 cover art (``artData``) on every active
# stream, so a normal body is several MB (seen at ~6.6 MB on the hub). Read up
# to this cap so a runaway or hostile body cannot exhaust memory, but keep it
# well above the real size so a genuine status is never rejected.
_MAX_STATUS_BYTES = 16 * 1024 * 1024


def fetch_status(client: httpx.Client, host: str, port: int) -> dict:
    """Call ``Server.GetStatus`` on the hub and return the JSON-RPC ``result``.

    The body is read with a size cap and parsed leniently: real responses
    contain raw control characters that a strict ``json.loads`` rejects. A body
    that is too large, truncated, or otherwise unparseable raises
    ``httpx.DecodingError``, the module's normal error type, so the app's guard
    logs once and falls through to art instead of crashing the loop.
    """
    with client.stream(
        "POST",
        f"http://{host}:{port}/jsonrpc",
        json={"id": 1, "jsonrpc": "2.0", "method": "Server.GetStatus"},
        timeout=_TIMEOUT,
    ) as response:
        response.raise_for_status()
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_bytes():
            total += len(chunk)
            if total > _MAX_STATUS_BYTES:
                raise httpx.DecodingError(
                    "Server.GetStatus body exceeded the size cap",
                    request=response.request,
                )
            chunks.append(chunk)
        request = response.request

    text = b"".join(chunks).decode("utf-8", errors="replace")
    try:
        payload = json.loads(text, strict=False)
        return payload["result"]
    except (json.JSONDecodeError, KeyError, TypeError) as error:
        raise httpx.DecodingError(
            f"Server.GetStatus returned an unparseable body: {error}",
            request=request,
        ) from error


def parse_now_playing(status: dict) -> NowPlaying | None:
    """Reduce a ``Server.GetStatus`` result to the current track, or ``None``."""
    server = status.get("server", {})
    streams = server.get("streams", [])
    groups = server.get("groups", [])

    for stream in streams:
        if stream.get("status") != "playing":
            continue
        stream_id = stream.get("id")
        stream_groups = [
            group
            for group in groups
            if group.get("stream_id") == stream_id and not group.get("muted", False)
        ]
        if not stream_groups:
            continue

        metadata = stream.get("properties", {}).get("metadata", {})
        title = metadata.get("title")
        if not title:
            return None

        rooms = _rooms(stream_groups)
        if not rooms:
            return None

        artist = _artist(metadata.get("artist"))
        return NowPlaying(
            title=title,
            artist=artist,
            album=metadata.get("album"),
            art_url=metadata.get("artUrl"),
            rooms=rooms,
        )

    return None


def _artist(value: object) -> str:
    """Coerce the ``artist`` metadata field to a display string.

    Snapcast sends an array normally, but a sender may put a plain string or
    ``null`` there. A list joins with ``", "``, a string passes through, and
    anything else (``null`` included) becomes an empty string.
    """
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    if isinstance(value, str):
        return value
    return ""


def _rooms(stream_groups: list[dict]) -> tuple[str, ...]:
    """The names of the unmuted, connected clients across ``stream_groups``."""
    names: list[str] = []
    for group in stream_groups:
        for client in group.get("clients", []):
            if not client.get("connected", False):
                continue
            config = client.get("config", {})
            if config.get("volume", {}).get("muted", False):
                continue
            name = config.get("name") or client.get("host", {}).get("name", "")
            if name:
                names.append(name)
    return tuple(names)
