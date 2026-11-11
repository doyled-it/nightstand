from __future__ import annotations

import httpx

from nightstand.sources.artcontrol import ArtControl, fetch_control

HOST = "house.local"
FRAME = "nightstand"


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _control_doc(**command) -> dict:
    doc = {
        "settings": {
            "rotate_minutes": 120,
            "folders_enabled": ["Collection", "Photos"],
        },
        "command": None,
    }
    if command:
        doc["command"] = command
    return doc


def test_parses_settings_and_a_command():
    doc = _control_doc(
        seq=7, action="next", piece=None, issued_at="2026-11-09T06:59:10-08:00"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/art-control.json"
        return httpx.Response(200, json=doc)

    with _client(handler) as client:
        control = fetch_control(client, HOST, FRAME)

    assert control == ArtControl(
        rotate_minutes=120,
        folders_enabled=("Collection", "Photos"),
        seq=7,
        action="next",
        piece=None,
        min_aspect=None,
    )


def test_parses_min_aspect():
    doc = {
        "settings": {
            "rotate_minutes": 60,
            "folders_enabled": ["Collection"],
            "min_aspect": 1.1,
        }
    }

    with _client(lambda r: httpx.Response(200, json=doc)) as client:
        control = fetch_control(client, HOST, FRAME)

    assert control is not None
    assert control.min_aspect == 1.1


def test_missing_min_aspect_is_none():
    with _client(lambda r: httpx.Response(200, json=_control_doc())) as client:
        control = fetch_control(client, HOST, FRAME)

    assert control is not None
    assert control.min_aspect is None


def test_bad_min_aspect_is_none():
    # A non-positive or non-numeric value (or a bool) is dropped, so the frame
    # falls back to its local config rather than a nonsense cutoff.
    for bad in ("1.1", 0, -1, True, None):
        doc = {
            "settings": {
                "rotate_minutes": 60,
                "folders_enabled": ["Collection"],
                "min_aspect": bad,
            }
        }
        with _client(lambda r, doc=doc: httpx.Response(200, json=doc)) as client:
            control = fetch_control(client, HOST, FRAME)
        assert control is not None
        assert control.min_aspect is None


def test_show_command_carries_the_piece_id():
    doc = _control_doc(seq=12, action="show", piece="Photos/beach.jpg")

    with _client(lambda r: httpx.Response(200, json=doc)) as client:
        control = fetch_control(client, HOST, FRAME)

    assert control is not None
    assert control.action == "show"
    assert control.piece == "Photos/beach.jpg"


def test_null_command_gives_no_seq_or_action():
    with _client(lambda r: httpx.Response(200, json=_control_doc())) as client:
        control = fetch_control(client, HOST, FRAME)

    assert control is not None
    assert control.seq is None
    assert control.action is None
    assert control.piece is None
    assert control.rotate_minutes == 120
    assert control.folders_enabled == ("Collection", "Photos")


def test_invalid_interval_falls_back_to_the_default():
    doc = {"settings": {"rotate_minutes": 7, "folders_enabled": ["Collection"]}}

    with _client(lambda r: httpx.Response(200, json=doc)) as client:
        control = fetch_control(client, HOST, FRAME)

    assert control is not None
    assert control.rotate_minutes == 60


def test_missing_file_is_none():
    with _client(lambda r: httpx.Response(404)) as client:
        assert fetch_control(client, HOST, FRAME) is None


def test_unreachable_hub_is_none():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("hub is down")

    with _client(handler) as client:
        assert fetch_control(client, HOST, FRAME) is None


def test_non_json_body_is_none():
    with _client(lambda r: httpx.Response(200, content=b"<html>nope</html>")) as client:
        assert fetch_control(client, HOST, FRAME) is None


def test_missing_settings_is_none():
    with _client(lambda r: httpx.Response(200, json={"command": None})) as client:
        assert fetch_control(client, HOST, FRAME) is None


# --- per-frame command scoping ------------------------------------------------



def test_command_scoped_to_this_frame_applies():
    doc = _control_doc(seq=7, action="next", piece=None, frame="nightstand")
    with _client(lambda r: httpx.Response(200, json=doc)) as client:
        control = fetch_control(client, HOST, FRAME)

    assert control is not None
    assert control.seq == 7
    assert control.action == "next"


def test_command_scoped_to_another_frame_is_ignored():
    # A command aimed at the office never moves the nightstand: it reads as no
    # command, so the rotation state machine does nothing.
    doc = _control_doc(seq=7, action="next", piece=None, frame="office")
    with _client(lambda r: httpx.Response(200, json=doc)) as client:
        control = fetch_control(client, HOST, FRAME)

    assert control is not None
    assert control.seq is None
    assert control.action is None
    assert control.piece is None
    # Shared settings still apply.
    assert control.rotate_minutes == 120


def test_command_without_a_frame_applies_to_any_frame():
    # No frame is the compatible default: the command reaches every frame.
    doc = _control_doc(seq=8, action="previous", piece=None)
    with _client(lambda r: httpx.Response(200, json=doc)) as client:
        control = fetch_control(client, HOST, "office")

    assert control is not None
    assert control.seq == 8
    assert control.action == "previous"


def test_null_frame_applies_to_any_frame():
    doc = _control_doc(seq=9, action="next", piece=None, frame=None)
    with _client(lambda r: httpx.Response(200, json=doc)) as client:
        control = fetch_control(client, HOST, "office")

    assert control is not None
    assert control.seq == 9


def test_bad_action_is_dropped_but_seq_is_kept():
    doc = _control_doc(seq=4, action="explode", piece=None)

    with _client(lambda r: httpx.Response(200, json=doc)) as client:
        control = fetch_control(client, HOST, FRAME)

    assert control is not None
    assert control.seq == 4
    assert control.action is None
