from __future__ import annotations

import httpx

from nightstand.sources.artcontrol import ArtControl, fetch_control

HOST = "house.local"


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
        control = fetch_control(client, HOST)

    assert control == ArtControl(
        rotate_minutes=120,
        folders_enabled=("Collection", "Photos"),
        seq=7,
        action="next",
        piece=None,
    )


def test_show_command_carries_the_piece_id():
    doc = _control_doc(seq=12, action="show", piece="Photos/beach.jpg")

    with _client(lambda r: httpx.Response(200, json=doc)) as client:
        control = fetch_control(client, HOST)

    assert control is not None
    assert control.action == "show"
    assert control.piece == "Photos/beach.jpg"


def test_null_command_gives_no_seq_or_action():
    with _client(lambda r: httpx.Response(200, json=_control_doc())) as client:
        control = fetch_control(client, HOST)

    assert control is not None
    assert control.seq is None
    assert control.action is None
    assert control.piece is None
    assert control.rotate_minutes == 120
    assert control.folders_enabled == ("Collection", "Photos")


def test_invalid_interval_falls_back_to_the_default():
    doc = {"settings": {"rotate_minutes": 7, "folders_enabled": ["Collection"]}}

    with _client(lambda r: httpx.Response(200, json=doc)) as client:
        control = fetch_control(client, HOST)

    assert control is not None
    assert control.rotate_minutes == 60


def test_missing_file_is_none():
    with _client(lambda r: httpx.Response(404)) as client:
        assert fetch_control(client, HOST) is None


def test_unreachable_hub_is_none():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("hub is down")

    with _client(handler) as client:
        assert fetch_control(client, HOST) is None


def test_non_json_body_is_none():
    with _client(lambda r: httpx.Response(200, content=b"<html>nope</html>")) as client:
        assert fetch_control(client, HOST) is None


def test_missing_settings_is_none():
    with _client(lambda r: httpx.Response(200, json={"command": None})) as client:
        assert fetch_control(client, HOST) is None


def test_bad_action_is_dropped_but_seq_is_kept():
    doc = _control_doc(seq=4, action="explode", piece=None)

    with _client(lambda r: httpx.Response(200, json=doc)) as client:
        control = fetch_control(client, HOST)

    assert control is not None
    assert control.seq == 4
    assert control.action is None
