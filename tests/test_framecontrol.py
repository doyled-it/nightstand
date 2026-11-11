from __future__ import annotations

from datetime import datetime

import httpx

from nightstand.sources.framecontrol import (
    MorningWeather,
    Pin,
    fetch_control,
)

HOST = "house.local"


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _serve(doc):
    return _client(lambda r: httpx.Response(200, json=doc))


def _full_doc(**overrides) -> dict:
    doc = {
        "settings": {
            "now_playing_kinds": ["music"],
            "quiet_hours": {"start": "22:00", "end": "06:00"},
            "morning_weather": {
                "enabled": True,
                "weekday": ["06:00", "08:30"],
                "weekend": ["07:00", "09:00"],
            },
        },
        "pin": None,
    }
    doc.update(overrides)
    return doc


# --- settings -----------------------------------------------------------------


def test_parses_full_settings_and_no_pin():
    with _serve(_full_doc()) as client:
        control = fetch_control(client, HOST)

    assert control is not None
    assert control.settings.now_playing_kinds == ("music",)
    assert control.settings.quiet_hours == ("22:00", "06:00")
    assert control.settings.morning_weather == MorningWeather(
        enabled=True, weekday=("06:00", "08:30"), weekend=("07:00", "09:00")
    )
    assert control.pin is None


def test_now_playing_kinds_keeps_only_the_three_valid_kinds():
    doc = _full_doc()
    doc["settings"]["now_playing_kinds"] = ["music", "podcast", "audiobook", "noise"]
    with _serve(doc) as client:
        control = fetch_control(client, HOST)

    assert control is not None
    assert control.settings.now_playing_kinds == ("music", "podcast", "audiobook")


def test_missing_now_playing_kinds_defaults_to_music():
    doc = _full_doc()
    del doc["settings"]["now_playing_kinds"]
    with _serve(doc) as client:
        control = fetch_control(client, HOST)

    assert control is not None
    assert control.settings.now_playing_kinds == ("music",)


def test_explicit_empty_now_playing_kinds_is_respected():
    # The owner can choose that no audio takes the frame; an explicit empty list
    # is honoured, unlike a missing or garbage value which defaults to music.
    doc = _full_doc()
    doc["settings"]["now_playing_kinds"] = []
    with _serve(doc) as client:
        control = fetch_control(client, HOST)

    assert control is not None
    assert control.settings.now_playing_kinds == ()


def test_garbage_now_playing_kinds_defaults_to_music():
    doc = _full_doc()
    doc["settings"]["now_playing_kinds"] = "music"  # not a list
    with _serve(doc) as client:
        control = fetch_control(client, HOST)

    assert control is not None
    assert control.settings.now_playing_kinds == ("music",)


def test_bad_quiet_hours_falls_back_to_none():
    for bad in (
        {"start": "22:00"},  # missing end
        {"start": "25:00", "end": "06:00"},  # bad hour
        {"start": "22:60", "end": "06:00"},  # bad minute
        {"start": "22:00", "end": 6},  # not a string
        "22:00-06:00",  # not an object
    ):
        doc = _full_doc()
        doc["settings"]["quiet_hours"] = bad
        with _serve(doc) as client:
            control = fetch_control(client, HOST)
        assert control is not None
        assert control.settings.quiet_hours is None, bad


def test_morning_weather_disabled_is_parsed():
    doc = _full_doc()
    doc["settings"]["morning_weather"]["enabled"] = False
    with _serve(doc) as client:
        control = fetch_control(client, HOST)

    assert control is not None
    assert control.settings.morning_weather is not None
    assert control.settings.morning_weather.enabled is False


def test_bad_morning_weather_falls_back_to_none():
    for bad in (
        {"enabled": True, "weekday": ["06:00"], "weekend": ["07:00", "09:00"]},
        {"enabled": True, "weekday": ["06:00", "bad"], "weekend": ["07:00", "09:00"]},
        {"weekday": ["06:00", "08:30"], "weekend": ["07:00", "09:00"]},  # no enabled
        "morning",
    ):
        doc = _full_doc()
        doc["settings"]["morning_weather"] = bad
        with _serve(doc) as client:
            control = fetch_control(client, HOST)
        assert control is not None
        assert control.settings.morning_weather is None, bad


# --- pin ----------------------------------------------------------------------


def test_parses_an_art_pin_with_a_piece_and_until():
    doc = _full_doc(
        pin={
            "what": "art",
            "piece": "Photos/beach.jpg",
            "until": "2026-11-11T07:00:00-08:00",
            "seq": 4,
            "set_at": "2026-11-10T21:00:00-08:00",
        }
    )
    with _serve(doc) as client:
        control = fetch_control(client, HOST)

    assert control is not None
    assert control.pin == Pin(
        what="art",
        piece="Photos/beach.jpg",
        until=datetime.fromisoformat("2026-11-11T07:00:00-08:00"),
        seq=4,
        set_at=datetime.fromisoformat("2026-11-10T21:00:00-08:00"),
    )


def test_forever_pin_has_no_until():
    doc = _full_doc(
        pin={"what": "weather", "piece": None, "until": None, "seq": 1, "set_at": None}
    )
    with _serve(doc) as client:
        control = fetch_control(client, HOST)

    assert control is not None
    assert control.pin is not None
    assert control.pin.what == "weather"
    assert control.pin.until is None


def test_pin_with_unknown_what_is_dropped():
    doc = _full_doc(pin={"what": "banana", "piece": None, "seq": 2})
    with _serve(doc) as client:
        control = fetch_control(client, HOST)

    assert control is not None
    assert control.pin is None


def test_pin_without_a_seq_is_dropped():
    # Without a sequence number a frame cannot tell a change, so the pin is unusable.
    doc = _full_doc(pin={"what": "art", "piece": "a.jpg"})
    with _serve(doc) as client:
        control = fetch_control(client, HOST)

    assert control is not None
    assert control.pin is None


def test_pin_with_bool_seq_is_dropped():
    doc = _full_doc(pin={"what": "art", "piece": "a.jpg", "seq": True})
    with _serve(doc) as client:
        control = fetch_control(client, HOST)

    assert control is not None
    assert control.pin is None


def test_pin_with_unparseable_until_treated_as_forever():
    doc = _full_doc(
        pin={"what": "art", "piece": "a.jpg", "until": "whenever", "seq": 3}
    )
    with _serve(doc) as client:
        control = fetch_control(client, HOST)

    assert control is not None
    assert control.pin is not None
    assert control.pin.until is None


# --- the file itself ----------------------------------------------------------


def test_missing_file_is_none():
    with _client(lambda r: httpx.Response(404)) as client:
        assert fetch_control(client, HOST) is None


def test_unreachable_hub_is_none():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("hub is down")

    with _client(handler) as client:
        assert fetch_control(client, HOST) is None


def test_non_json_body_is_none():
    with _client(lambda r: httpx.Response(200, content=b"<html>no</html>")) as client:
        assert fetch_control(client, HOST) is None


def test_missing_settings_is_none():
    with _client(lambda r: httpx.Response(200, json={"pin": None})) as client:
        assert fetch_control(client, HOST) is None


def test_requests_the_frame_control_path():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        return httpx.Response(200, json=_full_doc())

    with _client(handler) as client:
        fetch_control(client, HOST)

    assert seen["path"] == "/frame-control.json"
