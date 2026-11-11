from __future__ import annotations

from datetime import datetime

import httpx

from nightstand.sources.framecontrol import (
    MorningWeather,
    Pin,
    WeatherLocation,
    fetch_control,
)

HOST = "house.local"
FRAME = "nightstand"


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
        control = fetch_control(client, HOST, FRAME)

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
        control = fetch_control(client, HOST, FRAME)

    assert control is not None
    assert control.settings.now_playing_kinds == ("music", "podcast", "audiobook")


def test_missing_now_playing_kinds_defaults_to_music():
    doc = _full_doc()
    del doc["settings"]["now_playing_kinds"]
    with _serve(doc) as client:
        control = fetch_control(client, HOST, FRAME)

    assert control is not None
    assert control.settings.now_playing_kinds == ("music",)


def test_explicit_empty_now_playing_kinds_is_respected():
    # The owner can choose that no audio takes the frame; an explicit empty list
    # is honoured, unlike a missing or garbage value which defaults to music.
    doc = _full_doc()
    doc["settings"]["now_playing_kinds"] = []
    with _serve(doc) as client:
        control = fetch_control(client, HOST, FRAME)

    assert control is not None
    assert control.settings.now_playing_kinds == ()


def test_garbage_now_playing_kinds_defaults_to_music():
    doc = _full_doc()
    doc["settings"]["now_playing_kinds"] = "music"  # not a list
    with _serve(doc) as client:
        control = fetch_control(client, HOST, FRAME)

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
            control = fetch_control(client, HOST, FRAME)
        assert control is not None
        assert control.settings.quiet_hours is None, bad


def test_morning_weather_disabled_is_parsed():
    doc = _full_doc()
    doc["settings"]["morning_weather"]["enabled"] = False
    with _serve(doc) as client:
        control = fetch_control(client, HOST, FRAME)

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
            control = fetch_control(client, HOST, FRAME)
        assert control is not None
        assert control.settings.morning_weather is None, bad


# --- weather location ---------------------------------------------------------


def test_missing_weather_location_is_none():
    # No key means "keep the local [location]".
    with _serve(_full_doc()) as client:
        control = fetch_control(client, HOST, FRAME)

    assert control is not None
    assert control.settings.weather_location is None


def test_null_weather_location_is_none():
    doc = _full_doc()
    doc["settings"]["weather_location"] = None
    with _serve(doc) as client:
        control = fetch_control(client, HOST, FRAME)

    assert control is not None
    assert control.settings.weather_location is None


def test_parses_a_full_weather_location():
    # A well-known city centre, never the household's real location.
    doc = _full_doc()
    doc["settings"]["weather_location"] = {
        "name": "San Diego, California, United States",
        "latitude": 32.72,
        "longitude": -117.16,
        "timezone": "America/Los_Angeles",
    }
    with _serve(doc) as client:
        control = fetch_control(client, HOST, FRAME)

    assert control is not None
    assert control.settings.weather_location == WeatherLocation(
        name="San Diego, California, United States",
        latitude=32.72,
        longitude=-117.16,
        timezone="America/Los_Angeles",
    )


def test_weather_location_without_a_name_is_kept():
    doc = _full_doc()
    doc["settings"]["weather_location"] = {
        "latitude": 51.51,
        "longitude": -0.13,
        "timezone": "Europe/London",
    }
    with _serve(doc) as client:
        control = fetch_control(client, HOST, FRAME)

    assert control is not None
    assert control.settings.weather_location is not None
    assert control.settings.weather_location.name is None
    assert control.settings.weather_location.timezone == "Europe/London"


def test_bad_weather_location_falls_back_to_none():
    for bad in (
        {"latitude": 91.0, "longitude": 0.0, "timezone": "UTC"},  # lat too high
        {"latitude": -90.1, "longitude": 0.0, "timezone": "UTC"},  # lat too low
        {"latitude": 0.0, "longitude": 181.0, "timezone": "UTC"},  # lon too high
        {"latitude": 0.0, "longitude": -181.0, "timezone": "UTC"},  # lon too low
        {"latitude": 0.0, "longitude": 0.0, "timezone": "Mars/Phobos"},  # bad zone
        {"latitude": 0.0, "longitude": 0.0, "timezone": ""},  # empty zone
        {"latitude": 0.0, "longitude": 0.0},  # no timezone
        {"longitude": 0.0, "timezone": "UTC"},  # no latitude
        {"latitude": "32.72", "longitude": -117.16, "timezone": "UTC"},  # lat a string
        {"latitude": True, "longitude": 0.0, "timezone": "UTC"},  # lat a bool
        {"latitude": 10**400, "longitude": 0.0, "timezone": "UTC"},  # overflows float
        "San Diego",  # not an object
    ):
        doc = _full_doc()
        doc["settings"]["weather_location"] = bad
        with _serve(doc) as client:
            control = fetch_control(client, HOST, FRAME)
        assert control is not None
        assert control.settings.weather_location is None, bad


def test_oversized_coordinate_drops_only_the_location_not_the_document():
    # A huge JSON integer overflows float(); it must drop only weather_location,
    # leaving the rest of the control document (kinds, quiet hours, pin) intact
    # rather than raising and losing the whole document for the cycle.
    doc = _full_doc()
    doc["settings"]["weather_location"] = {
        "latitude": 10**400,
        "longitude": 0.0,
        "timezone": "UTC",
    }
    with _serve(doc) as client:
        control = fetch_control(client, HOST, FRAME)

    assert control is not None
    assert control.settings.weather_location is None
    assert control.settings.now_playing_kinds == ("music",)
    assert control.settings.quiet_hours == ("22:00", "06:00")


def test_integer_coordinates_are_accepted():
    # JSON may carry whole-number coordinates as ints; they are valid.
    doc = _full_doc()
    doc["settings"]["weather_location"] = {
        "latitude": 0,
        "longitude": 0,
        "timezone": "UTC",
    }
    with _serve(doc) as client:
        control = fetch_control(client, HOST, FRAME)

    assert control is not None
    assert control.settings.weather_location is not None
    assert control.settings.weather_location.latitude == 0.0


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
        control = fetch_control(client, HOST, FRAME)

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
        control = fetch_control(client, HOST, FRAME)

    assert control is not None
    assert control.pin is not None
    assert control.pin.what == "weather"
    assert control.pin.until is None


def test_pin_with_unknown_what_is_dropped():
    doc = _full_doc(pin={"what": "banana", "piece": None, "seq": 2})
    with _serve(doc) as client:
        control = fetch_control(client, HOST, FRAME)

    assert control is not None
    assert control.pin is None


def test_pin_without_a_seq_is_dropped():
    # Without a sequence number a frame cannot tell a change, so the pin is unusable.
    doc = _full_doc(pin={"what": "art", "piece": "a.jpg"})
    with _serve(doc) as client:
        control = fetch_control(client, HOST, FRAME)

    assert control is not None
    assert control.pin is None


def test_pin_with_bool_seq_is_dropped():
    doc = _full_doc(pin={"what": "art", "piece": "a.jpg", "seq": True})
    with _serve(doc) as client:
        control = fetch_control(client, HOST, FRAME)

    assert control is not None
    assert control.pin is None


def test_naive_until_is_rejected_so_the_pin_does_not_expire():
    # A timestamp with no UTC offset is naive; comparing it with the aware clock
    # would raise, so the stamp is rejected (treated as no expiry) rather than
    # reaching the loop. The pin itself is kept, like an unparseable until.
    doc = _full_doc(
        pin={"what": "art", "piece": "a.jpg", "until": "2026-11-11T07:00:00", "seq": 4}
    )
    with _serve(doc) as client:
        control = fetch_control(client, HOST, FRAME)

    assert control is not None
    assert control.pin is not None
    assert control.pin.until is None


def test_naive_set_at_is_rejected():
    doc = _full_doc(
        pin={
            "what": "weather",
            "piece": None,
            "until": None,
            "seq": 2,
            "set_at": "2026-11-10T21:00:00",  # naive, no offset
        }
    )
    with _serve(doc) as client:
        control = fetch_control(client, HOST, FRAME)

    assert control is not None
    assert control.pin is not None
    assert control.pin.set_at is None


def test_all_invalid_kinds_collapse_to_empty():
    # A list naming only unknown kinds filters to empty, honouring the explicit
    # (if nonsensical) list, which silences the now-playing screen. A non-list
    # value defaults to music instead; this is a deliberate, documented choice.
    doc = _full_doc()
    doc["settings"]["now_playing_kinds"] = ["noise", "static"]
    with _serve(doc) as client:
        control = fetch_control(client, HOST, FRAME)

    assert control is not None
    assert control.settings.now_playing_kinds == ()


def test_pin_with_unparseable_until_treated_as_forever():
    doc = _full_doc(
        pin={"what": "art", "piece": "a.jpg", "until": "whenever", "seq": 3}
    )
    with _serve(doc) as client:
        control = fetch_control(client, HOST, FRAME)

    assert control is not None
    assert control.pin is not None
    assert control.pin.until is None


# --- per-frame slice (frames map) ---------------------------------------------



def _frames_doc(frames: dict, **settings_overrides) -> dict:
    settings = {
        "now_playing_kinds": ["music"],
        "quiet_hours": {"start": "22:00", "end": "06:00"},
    }
    settings.update(settings_overrides)
    return {"settings": settings, "frames": frames}


def test_uses_this_frames_quiet_hours_and_pin():
    doc = _frames_doc(
        {
            "nightstand": {
                "quiet_hours": {"start": "23:00", "end": "07:00"},
                "pin": {"what": "art", "piece": "a.jpg", "seq": 5},
            },
            "office": {"quiet_hours": {"enabled": False}, "pin": None},
        }
    )
    with _serve(doc) as client:
        control = fetch_control(client, HOST, FRAME)

    assert control is not None
    # This frame's own override wins over the shared settings quiet hours.
    assert control.settings.quiet_hours == ("23:00", "07:00")
    assert control.pin is not None
    assert control.pin.what == "art"
    assert control.pin.seq == 5


def test_ignores_another_frames_pin():
    doc = _frames_doc(
        {
            "nightstand": {"quiet_hours": None, "pin": None},
            "office": {"quiet_hours": None, "pin": {"what": "weather", "seq": 9}},
        }
    )
    with _serve(doc) as client:
        control = fetch_control(client, HOST, FRAME)

    assert control is not None
    assert control.pin is None
    # A null per-frame override falls back to the shared quiet hours.
    assert control.settings.quiet_hours == ("22:00", "06:00")


def test_quiet_hours_off_is_a_zero_length_window():
    # {"enabled": false} means this frame keeps no quiet hours at all; encoded as
    # a zero-length window that in_quiet_hours reads as "never quiet".
    doc = _frames_doc({"nightstand": {"quiet_hours": {"enabled": False}, "pin": None}})
    with _serve(doc) as client:
        control = fetch_control(client, HOST, FRAME)

    assert control is not None
    assert control.settings.quiet_hours == ("00:00", "00:00")


def test_null_per_frame_quiet_hours_falls_back_to_shared():
    doc = _frames_doc({"nightstand": {"quiet_hours": None, "pin": None}})
    with _serve(doc) as client:
        control = fetch_control(client, HOST, FRAME)

    assert control is not None
    assert control.settings.quiet_hours == ("22:00", "06:00")


def test_frame_absent_from_map_gets_shared_quiet_and_no_pin():
    # The hub does not list this frame yet: it keeps the shared quiet hours and no
    # pin, rather than crashing.
    doc = _frames_doc({"office": {"quiet_hours": {"enabled": False}, "pin": None}})
    with _serve(doc) as client:
        control = fetch_control(client, HOST, FRAME)

    assert control is not None
    assert control.settings.quiet_hours == ("22:00", "06:00")
    assert control.pin is None


def test_malformed_per_frame_quiet_hours_falls_back_to_shared():
    doc = _frames_doc({"nightstand": {"quiet_hours": {"start": "25:00"}, "pin": None}})
    with _serve(doc) as client:
        control = fetch_control(client, HOST, FRAME)

    assert control is not None
    assert control.settings.quiet_hours == ("22:00", "06:00")


# --- old single-frame shape (top-level pin) during rollout --------------------


def test_old_shape_top_level_pin_applies_to_the_nightstand():
    # A document with no frames map is the old contract; its top-level pin is the
    # nightstand's, so the nightstand keeps working if the hub updates last.
    doc = _full_doc(pin={"what": "art", "piece": "a.jpg", "seq": 3})
    with _serve(doc) as client:
        control = fetch_control(client, HOST, "nightstand")

    assert control is not None
    assert control.pin is not None
    assert control.pin.what == "art"
    assert control.pin.seq == 3
    assert control.settings.quiet_hours == ("22:00", "06:00")


def test_old_shape_top_level_pin_ignored_for_another_frame():
    doc = _full_doc(pin={"what": "art", "piece": "a.jpg", "seq": 3})
    with _serve(doc) as client:
        control = fetch_control(client, HOST, "office")

    assert control is not None
    assert control.pin is None


# --- the file itself ----------------------------------------------------------


def test_missing_file_is_none():
    with _client(lambda r: httpx.Response(404)) as client:
        assert fetch_control(client, HOST, FRAME) is None


def test_unreachable_hub_is_none():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("hub is down")

    with _client(handler) as client:
        assert fetch_control(client, HOST, FRAME) is None


def test_non_json_body_is_none():
    with _client(lambda r: httpx.Response(200, content=b"<html>no</html>")) as client:
        assert fetch_control(client, HOST, FRAME) is None


def test_missing_settings_is_none():
    with _client(lambda r: httpx.Response(200, json={"pin": None})) as client:
        assert fetch_control(client, HOST, FRAME) is None


def test_requests_the_frame_control_path():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        return httpx.Response(200, json=_full_doc())

    with _client(handler) as client:
        fetch_control(client, HOST, FRAME)

    assert seen["path"] == "/frame-control.json"
