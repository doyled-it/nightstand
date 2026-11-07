from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import httpx
import pytest

from nightstand.models import NowPlaying
from nightstand.sources import snapserver

FIXTURES = Path(__file__).parent / "fixtures" / "snapserver"


def _result(name: str) -> dict:
    """The JSON-RPC ``result`` object from a recorded Server.GetStatus fixture."""
    return json.loads((FIXTURES / name).read_text(), strict=False)["result"]


def _fetch_body(body: bytes) -> dict:
    """Run ``fetch_status`` against a response whose raw body is ``body``."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as client:
        return snapserver.fetch_status(client, "hub.local", 1780)


def test_parse_airplay_returns_title_artists_and_rooms():
    np = snapserver.parse_now_playing(_result("playing_airplay.json"))

    assert np is not None
    assert np.title == "Tidal Drift"
    assert np.artist == "Nova Reef, The Saltwater Choir"
    assert np.album == "Blue Hour"
    assert np.art_url == "http://house.local/art/blue-hour.jpg"
    assert np.rooms == ("Living room", "Bedroom")


def test_parse_bluetooth_without_art_falls_back_to_none_art_url():
    np = snapserver.parse_now_playing(_result("playing_bluetooth.json"))

    assert np is not None
    assert np.title == "Paper Lanterns"
    assert np.artist == "Marsh & the Meadow"
    assert np.art_url is None
    assert np.rooms == ("Office",)


def _status_with_metadata(metadata: dict) -> dict:
    """A minimal GetStatus result: one playing stream to one unmuted room."""
    return {
        "server": {
            "streams": [
                {
                    "id": "s1",
                    "status": "playing",
                    "properties": {"metadata": metadata},
                }
            ],
            "groups": [
                {
                    "stream_id": "s1",
                    "muted": False,
                    "clients": [
                        {
                            "connected": True,
                            "config": {
                                "name": "Office",
                                "volume": {"muted": False},
                            },
                        }
                    ],
                }
            ],
        }
    }


def test_artist_list_is_joined_with_commas():
    status = _status_with_metadata(
        {"title": "Track", "artist": ["Nova Reef", "The Saltwater Choir"]}
    )
    np = snapserver.parse_now_playing(status)

    assert np is not None
    assert np.artist == "Nova Reef, The Saltwater Choir"


def test_artist_plain_string_is_kept_as_is():
    status = _status_with_metadata({"title": "Track", "artist": "Lone Sender"})
    np = snapserver.parse_now_playing(status)

    assert np is not None
    assert np.artist == "Lone Sender"


def test_artist_null_or_missing_becomes_empty_string():
    null_status = _status_with_metadata({"title": "Track", "artist": None})
    np_null = snapserver.parse_now_playing(null_status)
    assert np_null is not None
    assert np_null.artist == ""

    missing_status = _status_with_metadata({"title": "Track"})
    np_missing = snapserver.parse_now_playing(missing_status)
    assert np_missing is not None
    assert np_missing.artist == ""


def test_parse_idle_returns_none():
    assert snapserver.parse_now_playing(_result("idle.json")) is None


def test_parse_all_muted_returns_none():
    assert snapserver.parse_now_playing(_result("all_muted.json")) is None


def test_fetch_status_posts_getstatus_and_returns_result():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["method"] = request.method
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"id": 1, "jsonrpc": "2.0", "result": {"server": {"ok": True}}},
        )

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as client:
        result = snapserver.fetch_status(client, "hub.local", 1780)

    assert result == {"server": {"ok": True}}
    assert captured["method"] == "POST"
    assert captured["url"] == "http://hub.local:1780/jsonrpc"
    assert captured["body"] == {
        "id": 1,
        "jsonrpc": "2.0",
        "method": "Server.GetStatus",
    }


def test_fetch_status_parses_large_artdata_with_control_characters():
    # The real hub returns a multi-MB body carrying base64 cover art and raw
    # control characters, which a strict json.loads rejects. fetch_status must
    # read and parse it leniently so now playing still resolves.
    result = _fetch_body(
        (FIXTURES / "playing_airplay_artdata.json").read_bytes()
    )
    np = snapserver.parse_now_playing(result)

    assert np is not None
    assert np.title == "Tidal Drift"
    assert np.artist == "Nova Reef, The Saltwater Choir"
    assert np.album == "Blue Hour"
    assert np.art_url == "http://house.local/art/blue-hour.jpg"
    assert np.rooms == ("Living room",)


def test_now_playing_never_carries_art_data():
    result = _fetch_body(
        (FIXTURES / "playing_airplay_artdata.json").read_bytes()
    )
    np = snapserver.parse_now_playing(result)

    assert np is not None
    # Only the small art URL reaches the screen state; the multi-KB base64
    # cover and its control characters never do.
    assert "artData" not in {f.name for f in dataclasses.fields(NowPlaying)}
    string_fields = [v for v in dataclasses.astuple(np) if isinstance(v, str)]
    assert all(len(value) < 256 for value in string_fields)
    assert all(
        not any(control in value for control in "\x00\x01\x02\x07\x1f")
        for value in string_fields
    )


def test_fetch_status_raises_module_error_on_truncated_body():
    # A truncated read ends mid-string (an unterminated JSON string). The
    # module must surface its normal error type so the app's guard logs once
    # and falls through, not an unexpected exception.
    truncated = (FIXTURES / "playing_airplay_artdata.json").read_bytes()[:2048]

    with pytest.raises(httpx.DecodingError):
        _fetch_body(truncated)
