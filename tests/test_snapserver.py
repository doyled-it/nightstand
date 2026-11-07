from __future__ import annotations

import json
from pathlib import Path

import httpx

from nightstand.sources import snapserver

FIXTURES = Path(__file__).parent / "fixtures" / "snapserver"


def _result(name: str) -> dict:
    """The JSON-RPC ``result`` object from a recorded Server.GetStatus fixture."""
    return json.loads((FIXTURES / name).read_text())["result"]


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
