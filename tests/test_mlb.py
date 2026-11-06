from __future__ import annotations

import io
import json
import warnings
from datetime import date
from pathlib import Path

import httpx
import pytest
from PIL import Image, ImageDraw

from nightstand.screens import game
from nightstand.sources import mlb
from nightstand.sources.mlb import GameState, Team

FIXTURES = Path(__file__).parent / "fixtures" / "mlb"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def schedule_client(payload: dict) -> httpx.Client:
    """A client whose every request returns ``payload`` as the schedule JSON."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "statsapi.mlb.com"
        assert request.url.path == "/api/v1/schedule"
        return httpx.Response(200, json=payload)

    return httpx.Client(transport=httpx.MockTransport(handler))


# --- parse_game ------------------------------------------------------------


def test_parse_scoring_play():
    g = mlb.parse_game(load("feed_scoring_play.json"))
    assert g == GameState(
        game_pk=823401,
        status="live",
        away=Team(id=135, abbrev="SD", runs=1),
        home=Team(id=134, abbrev="PIT", runs=0),
        inning=4,
        inning_state="Top",
        summary=(
            "Nick Castellanos doubles (2) on a sharp line drive to left "
            "fielder Bryan Reynolds. Manny Machado scores. Gavin Sheets to 3rd."
        ),
        ended_at=None,
    )
    assert mlb.state_label(g) == "Top 4th"


def test_parse_middle_inning():
    g = mlb.parse_game(load("feed_middle_inning.json"))
    assert g.status == "live"
    assert g.inning == 8
    assert g.inning_state == "Middle"
    assert g.away.runs == 5 and g.home.runs == 0
    assert g.summary == "SD scored 1 in the top 8th"
    assert mlb.state_label(g) == "Middle 8th"


def test_parse_end_inning():
    g = mlb.parse_game(load("feed_end_inning.json"))
    assert g.inning == 4
    assert g.inning_state == "End"
    assert g.summary == "No runs in the bottom 4th"
    assert mlb.state_label(g) == "End 4th"


def test_parse_final():
    g = mlb.parse_game(load("feed_final.json"))
    assert g.status == "final"
    assert g.away.runs == 5 and g.home.runs == 0
    assert g.summary == "Final: SD win 5-0"
    assert mlb.state_label(g) == "Final"
    assert g.ended_at is not None
    assert g.ended_at.year == 2026 and g.ended_at.month == 4 and g.ended_at.day == 7


def test_parse_preview_returns_none():
    assert mlb.parse_game(load("feed_preview.json")) is None


def test_summary_empty_without_scoring_play_in_current_half():
    # Current half is the top of the 5th; the only scoring play was the 3rd.
    feed = {
        "gamePk": 1,
        "gameData": {
            "status": {"abstractGameState": "Live", "detailedState": "In Progress"},
            "datetime": {"dateTime": "2026-04-06T22:40:00Z"},
            "teams": {
                "away": {"id": 135, "abbreviation": "SD"},
                "home": {"id": 134, "abbreviation": "PIT"},
            },
        },
        "liveData": {
            "linescore": {
                "currentInning": 5,
                "inningState": "Top",
                "teams": {"away": {"runs": 2}, "home": {"runs": 1}},
                "innings": [],
            },
            "plays": {
                "scoringPlays": [0],
                "allPlays": [
                    {
                        "result": {"description": "a third-inning homer"},
                        "about": {
                            "inning": 3,
                            "halfInning": "bottom",
                            "endTime": "2026-04-06T23:00:00Z",
                        },
                    }
                ],
            },
        },
    }
    g = mlb.parse_game(feed)
    assert g.summary == ""


# --- find_game -------------------------------------------------------------


def test_find_game_doubleheader_picks_live():
    with schedule_client(load("schedule_doubleheader.json")) as client:
        assert mlb.find_game(client, 135, date(2026, 7, 18)) == 779002


def test_find_game_postponed_is_no_game():
    with schedule_client(load("schedule_postponed.json")) as client:
        assert mlb.find_game(client, 135, date(2026, 5, 2)) is None


def test_find_game_prefers_latest_final():
    payload = {
        "dates": [
            {
                "games": [
                    {
                        "gamePk": 1,
                        "gameDate": "2026-07-18T17:10:00Z",
                        "status": {
                            "abstractGameState": "Final",
                            "detailedState": "Final",
                        },
                    },
                    {
                        "gamePk": 2,
                        "gameDate": "2026-07-18T23:40:00Z",
                        "status": {
                            "abstractGameState": "Final",
                            "detailedState": "Final",
                        },
                    },
                ]
            }
        ]
    }
    with schedule_client(payload) as client:
        assert mlb.find_game(client, 135, date(2026, 7, 18)) == 2


def test_find_game_no_games_returns_none():
    with schedule_client({"dates": []}) as client:
        assert mlb.find_game(client, 135, date(2026, 5, 2)) is None


# --- redraw key and helpers ------------------------------------------------


def _state(**kw) -> GameState:
    base = dict(
        game_pk=1,
        status="live",
        away=Team(id=135, abbrev="SD", runs=2),
        home=Team(id=134, abbrev="PIT", runs=1),
        inning=7,
        inning_state="Middle",
        summary="",
        ended_at=None,
    )
    base.update(kw)
    return GameState(**base)


def test_redraw_key_same_for_middle_and_bottom_of_an_inning():
    middle = _state(inning_state="Middle")
    bottom = _state(inning_state="Bottom")
    assert mlb.game_redraw_key(middle) == mlb.game_redraw_key(bottom)


def test_redraw_key_differs_after_a_run():
    before = _state(inning_state="Bottom")
    after = _state(inning_state="Bottom", home=Team(id=134, abbrev="PIT", runs=2))
    assert mlb.game_redraw_key(before) != mlb.game_redraw_key(after)


@pytest.mark.parametrize(
    "inning,state,expected",
    [
        (1, "Top", 0),
        (1, "Middle", 1),
        (1, "Bottom", 1),
        (1, "End", 2),
        (7, "Top", 12),
        (7, "End", 14),
    ],
)
def test_completed_halves(inning, state, expected):
    assert mlb.completed_halves(_state(inning=inning, inning_state=state)) == expected


# --- logo cache ------------------------------------------------------------


def _png(color: tuple[int, int, int] = (200, 40, 40)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (150, 150), color).save(buf, format="PNG")
    return buf.getvalue()


def test_logo_fetches_once_then_reads_cache(tmp_path):
    hosts: list[str] = []
    png = _png()

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        assert request.url.host == "midfield.mlbstatic.com"
        return httpx.Response(200, content=png, headers={"content-type": "image/png"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        cache = mlb.LogoCache(client, tmp_path)
        first = cache.for_team(135)
        second = cache.for_team(135)

    assert isinstance(first, Image.Image)
    assert isinstance(second, Image.Image)
    assert hosts.count("midfield.mlbstatic.com") == 1
    assert (tmp_path / "135.png").is_file()


def test_logo_missing_returns_none(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert mlb.LogoCache(client, tmp_path).for_team(999) is None


def _palette_png_with_byte_transparency() -> bytes:
    """A palette PNG whose transparency is expressed in bytes, like MLB's logos.

    Loading such an image and converting it later makes Pillow warn unless it is
    first converted to RGBA. Intermediate alpha values keep the transparency as
    bytes (not a single index) when the PNG is reopened, so the warning fires.
    """
    img = Image.new("P", (150, 150), 0)
    img.putpalette([255, 255, 255, 0, 0, 255, 0, 255, 0] + [0] * (256 * 3 - 9))
    ImageDraw.Draw(img).ellipse([10, 10, 140, 140], fill=1)
    ImageDraw.Draw(img).ellipse([60, 60, 90, 90], fill=2)
    img.info["transparency"] = bytes([0, 128, 255] + [255] * 253)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_logo_with_palette_transparency_loads_and_renders_without_warning(tmp_path):
    png = _palette_png_with_byte_transparency()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=png, headers={"content-type": "image/png"})

    # Any UserWarning (Pillow's palette-transparency one, in particular) becomes
    # an error, so decoding the logo and rendering it must stay pristine.
    with warnings.catch_warnings():
        warnings.simplefilter("error", UserWarning)
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            logo = mlb.LogoCache(client, tmp_path).for_team(135)
        assert logo is not None
        assert logo.mode == "RGBA"
        state = GameState(
            game_pk=1,
            status="live",
            away=Team(id=135, abbrev="SD", runs=3),
            home=Team(id=134, abbrev="PIT", runs=1),
            inning=7,
            inning_state="Bottom",
            summary="A scoring play",
            ended_at=None,
        )
        game.render(state, logo, logo)


@pytest.mark.parametrize(
    "n,expected",
    [
        (1, "1st"),
        (2, "2nd"),
        (3, "3rd"),
        (4, "4th"),
        (11, "11th"),
        (12, "12th"),
        (13, "13th"),
        (21, "21st"),
    ],
)
def test_ordinal(n, expected):
    assert mlb.ordinal(n) == expected
