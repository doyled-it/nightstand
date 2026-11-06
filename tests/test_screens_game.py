from __future__ import annotations

from PIL import Image, ImageDraw

from nightstand.screens import game
from nightstand.screens.common import HEIGHT, WIDTH
from nightstand.sources.mlb import GameState, Team

# Text-heavy screen: large, bold glyphs leave more anti-aliased edge pixels that
# snap to a different side of the six-color threshold across architectures, so
# the golden comparison needs more headroom than the default.
TOLERANCE = 0.03


def _logo(color: tuple[int, int, int]) -> Image.Image:
    """A stand-in for a real team logo: a solid disc on white, 150x150."""
    img = Image.new("RGB", (150, 150), (255, 255, 255))
    ImageDraw.Draw(img).ellipse([10, 10, 140, 140], fill=color)
    return img


def _live_game(
    summary: str = "Machado doubles to left, Tatis scores. SD leads.",
) -> GameState:
    return GameState(
        game_pk=823401,
        status="live",
        away=Team(id=135, abbrev="SD", runs=3),
        home=Team(id=134, abbrev="PIT", runs=1),
        inning=7,
        inning_state="Bottom",
        summary=summary,
        ended_at=None,
    )


def test_render_live_with_logos_matches_golden(assert_golden):
    g = _live_game()
    img = game.render(g, _logo((0, 0, 255)), _logo((0, 128, 0)))
    assert img.size == (WIDTH, HEIGHT)
    assert_golden(img, "game_live.png", TOLERANCE)


def test_render_final_without_logos_matches_golden(assert_golden):
    g = GameState(
        game_pk=823401,
        status="final",
        away=Team(id=135, abbrev="SD", runs=5),
        home=Team(id=134, abbrev="PIT", runs=0),
        inning=9,
        inning_state="Bottom",
        summary="Final: SD win 5-0",
        ended_at=None,
    )
    img = game.render(g, None, None)
    assert img.size == (WIDTH, HEIGHT)
    assert_golden(img, "game_final_no_logos.png", TOLERANCE)


def test_long_summary_stays_within_two_lines():
    # A long scoring description must be capped, not spill past the bottom band.
    long = (
        "Fernando Tatis Jr. homers (31) on a towering fly ball to deep left "
        "center field, scoring Manny Machado and Xander Bogaerts, and the San "
        "Diego Padres take a commanding lead over the Pittsburgh Pirates in a "
        "game that keeps turning on every single pitch of a long afternoon at "
        "Petco Park, where the crowd has been on its feet for the better part "
        "of an hour as the home side keeps adding on against a bullpen that "
        "has nothing left to give on this warm and restless September evening."
    )
    assert len(long) >= 400
    lines = game.summary_lines(long)
    assert len(lines) <= 2
    # Content was dropped to fit, so the last line is ellipsized rather than run over.
    assert lines[-1].endswith("…")
