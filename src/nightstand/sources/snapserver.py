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

import httpx

from nightstand.models import NowPlaying

_TIMEOUT = 5.0


def fetch_status(client: httpx.Client, host: str, port: int) -> dict:
    """Call ``Server.GetStatus`` on the hub and return the JSON-RPC ``result``."""
    response = client.post(
        f"http://{host}:{port}/jsonrpc",
        json={"id": 1, "jsonrpc": "2.0", "method": "Server.GetStatus"},
        timeout=_TIMEOUT,
    )
    response.raise_for_status()
    return response.json()["result"]


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

        artist = ", ".join(metadata.get("artist", []))
        return NowPlaying(
            title=title,
            artist=artist,
            album=metadata.get("album"),
            art_url=metadata.get("artUrl"),
            rooms=rooms,
        )

    return None


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
