from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from nightstand.models import NowPlaying
from nightstand.sources import hubnowplaying

FIXTURES = Path(__file__).parent / "fixtures" / "nowplaying"
PACIFIC = timezone(timedelta(hours=-8))


def _doc(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


# --- parse_now_playing -------------------------------------------------------


def test_parse_every_field_of_the_spec_example():
    np = hubnowplaying.parse_now_playing(_doc("spec_example.json"))

    assert np == NowPlaying(
        title="Episode 412: A Neutral Episode",
        artist="The Long Version",
        album="The Long Version",
        art_url="/cover/3f2a9b7c.jpg",
        rooms=("Bedroom", "Office"),
        kind="long_form",
        status="playing",
        duration=3120.0,
        position=1390.0,
        position_at=datetime(2026, 11, 8, 21, 13, 58, tzinfo=PACIFIC),
        started_at=datetime(2026, 11, 8, 20, 50, 48, tzinfo=PACIFIC),
    )


def test_parse_real_bluetooth_podcast_recording():
    np = hubnowplaying.parse_now_playing(_doc("bluetooth_podcast.json"))

    assert np is not None
    assert np.title.startswith("[AUDIO] Bonus #58")
    assert np.kind == "long_form"
    assert np.status == "playing"
    assert np.art_url is None
    assert np.rooms == ("Bedroom",)
    assert np.duration == 5845.0
    assert np.position == pytest.approx(3605.1398, rel=1e-6)


def test_track_null_gives_none():
    doc = {"track": None, "rooms": ["Bedroom"]}
    assert hubnowplaying.parse_now_playing(doc) is None


def test_empty_rooms_gives_none():
    doc = _doc("spec_example.json")
    doc["rooms"] = []
    assert hubnowplaying.parse_now_playing(doc) is None


def test_missing_title_gives_none():
    doc = _doc("spec_example.json")
    doc["track"]["title"] = None
    assert hubnowplaying.parse_now_playing(doc) is None
    doc["track"].pop("title")
    assert hubnowplaying.parse_now_playing(doc) is None


def test_missing_track_key_gives_none():
    assert hubnowplaying.parse_now_playing({"rooms": ["Bedroom"]}) is None
    assert hubnowplaying.parse_now_playing({}) is None


def test_title_without_artist_or_album_still_publishes():
    # Review Focus 2: internet radio and some apps send only a title. The track
    # must publish with an empty artist and no album, so the screen draws no
    # empty lines.
    doc = {
        "track": {"title": "Pure Tone Radio", "artist": None, "album": None},
        "rooms": ["Bedroom"],
    }
    np = hubnowplaying.parse_now_playing(doc)

    assert np is not None
    assert np.title == "Pure Tone Radio"
    assert np.artist == ""
    assert np.album is None


def test_malformed_field_values_degrade_to_none_without_raising():
    # A valid JSON object with garbage in the optional fields must not crash the
    # parse; the bad fields simply drop to None rather than taking the track down.
    doc = {
        "track": {
            "title": "Still Plays",
            "artist": "Someone",
            "duration": "not a number",
            "position": "nope",
            "position_at": "not a timestamp",
            "started_at": 12345,
        },
        "rooms": ["Bedroom"],
    }
    np = hubnowplaying.parse_now_playing(doc)

    assert np is not None
    assert np.duration is None
    assert np.position is None
    assert np.position_at is None
    assert np.started_at is None


# --- position_now ------------------------------------------------------------


def test_position_now_extrapolates_while_playing():
    np = NowPlaying(
        title="T",
        artist="A",
        status="playing",
        duration=3120.0,
        position=1390.0,
        position_at=datetime(2026, 11, 8, 21, 13, 58, tzinfo=PACIFIC),
    )
    now = datetime(2026, 11, 8, 21, 14, 58, tzinfo=PACIFIC)  # 60 s later

    assert np.position_now(now) == pytest.approx(1450.0)


def test_position_now_freezes_while_paused():
    np = NowPlaying(
        title="T",
        artist="A",
        status="paused",
        duration=3120.0,
        position=1390.0,
        position_at=datetime(2026, 11, 8, 21, 13, 58, tzinfo=PACIFIC),
    )
    now = datetime(2026, 11, 8, 21, 20, 58, tzinfo=PACIFIC)  # 7 min later

    assert np.position_now(now) == pytest.approx(1390.0)


def test_position_now_clamps_to_duration():
    np = NowPlaying(
        title="T",
        artist="A",
        status="playing",
        duration=100.0,
        position=90.0,
        position_at=datetime(2026, 11, 8, 21, 13, 58, tzinfo=PACIFIC),
    )
    now = datetime(2026, 11, 8, 21, 14, 58, tzinfo=PACIFIC)  # 60 s later, past end

    assert np.position_now(now) == pytest.approx(100.0)


def test_position_now_is_none_without_a_position():
    np = NowPlaying(title="T", artist="A", duration=100.0)
    assert np.position_now(datetime(2026, 11, 8, 21, 14, 58, tzinfo=PACIFIC)) is None


def test_position_now_without_position_at_returns_position():
    # A duration-only track (no anchor) reports its static position unchanged.
    np = NowPlaying(title="T", artist="A", status="playing", position=42.0)
    assert np.position_now(datetime.now(PACIFIC)) == pytest.approx(42.0)


# --- fetch_now_playing -------------------------------------------------------


def test_fetch_returns_parsed_json_on_200():
    payload = _doc("spec_example.json")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/now-playing.json"
        assert request.url.host == "house.local"
        return httpx.Response(200, json=payload)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        doc = hubnowplaying.fetch_now_playing(client, "house.local")

    assert doc == payload


def test_fetch_raises_on_non_200():
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(httpx.HTTPError):
            hubnowplaying.fetch_now_playing(client, "house.local")


def test_fetch_raises_on_bad_json():
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>not json</html>")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(json.JSONDecodeError):
            hubnowplaying.fetch_now_playing(client, "house.local")
