"""The Padres game screen: logos, very large run totals, state, and a summary.

Two columns fill the middle, away on the left and home on the right: each team's
logo above its run total in a huge black weight. Stacking the logo over the
number keeps the columns balanced whatever the score, where a wide double-digit
total set beside a logo would collide. The game state ("Top 7th", "Final") sits
top-left in red. A thin rule separates the scoreboard from one summary line along
the bottom: the scoring play that just happened, or what the half-inning that
just ended produced.

When a logo is missing the team's abbreviation stands in. Everything is drawn
anti-aliased, then the whole canvas is snapped to the six pure panel colors, so
the panel only ever receives six colors.
"""

from __future__ import annotations

from PIL import Image, ImageDraw

from nightstand.palette import PURE, to_six_colors
from nightstand.screens.common import HEIGHT, WIDTH
from nightstand.sources.mlb import GameState, Team, state_label
from nightstand.text import font, wrap

MARGIN = 28

STATE_SIZE = 44
STATE_TOP = 20

# Each team is a column centered on this x; the logo sits above the run total.
AWAY_CX = 205
HOME_CX = 595
LOGO_SIZE = 150
LOGO_TOP = 78
RUNS_CY = 308
RUNS_SIZE = 200
ABBREV_SIZE = 64

RULE_Y = 392
RULE_HEIGHT = 4

SUMMARY_SIZE = 30
SUMMARY_LINES = 2
SUMMARY_TOP = 402
SUMMARY_WIDTH = WIDTH - 2 * MARGIN


def render(
    game: GameState,
    away_logo: Image.Image | None,
    home_logo: Image.Image | None,
) -> Image.Image:
    """Render the game screen for ``game`` at 800x480."""
    canvas = Image.new("RGB", (WIDTH, HEIGHT), PURE["white"])
    draw = ImageDraw.Draw(canvas)

    draw.text(
        (MARGIN, STATE_TOP),
        state_label(game),
        font=font("black", STATE_SIZE),
        fill=PURE["red"],
        anchor="la",
    )

    runs_font = font("black", RUNS_SIZE)
    _draw_team(canvas, draw, away_logo, game.away, AWAY_CX, runs_font)
    _draw_team(canvas, draw, home_logo, game.home, HOME_CX, runs_font)

    draw.rectangle(
        [MARGIN, RULE_Y, WIDTH - MARGIN, RULE_Y + RULE_HEIGHT], fill=PURE["black"]
    )

    summary_font = font("regular", SUMMARY_SIZE)
    ascent, descent = summary_font.getmetrics()
    y = SUMMARY_TOP
    for line in summary_lines(game.summary):
        draw.text((MARGIN, y), line, font=summary_font, fill=PURE["black"], anchor="la")
        y += ascent + descent

    # Snap the anti-aliased text, logos, and rule so the panel sees six colors.
    return to_six_colors(canvas)


def summary_lines(summary: str) -> list[str]:
    """The summary wrapped to at most two lines at the screen's summary width."""
    scratch = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    return wrap(
        scratch, summary, font("regular", SUMMARY_SIZE), SUMMARY_WIDTH, SUMMARY_LINES
    )


def _draw_team(
    canvas: Image.Image,
    draw: ImageDraw.ImageDraw,
    logo: Image.Image | None,
    team: Team,
    center_x: int,
    runs_font,
) -> None:
    """Draw one column: the logo (or abbreviation) above the run total."""
    if logo is not None:
        resized = logo.convert("RGBA").resize(
            (LOGO_SIZE, LOGO_SIZE), Image.Resampling.LANCZOS
        )
        # Composite over white so a logo's transparent background reads as the
        # panel's background rather than a stray color when alpha is dropped.
        box = Image.new("RGB", (LOGO_SIZE, LOGO_SIZE), PURE["white"])
        box.paste(resized, (0, 0), resized)
        canvas.paste(box, (center_x - LOGO_SIZE // 2, LOGO_TOP))
    else:
        draw.text(
            (center_x, LOGO_TOP + LOGO_SIZE // 2),
            team.abbrev,
            font=font("bold", ABBREV_SIZE),
            fill=PURE["black"],
            anchor="mm",
        )
    draw.text(
        (center_x, RUNS_CY),
        str(team.runs),
        font=runs_font,
        fill=PURE["black"],
        anchor="mm",
    )
