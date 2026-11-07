from __future__ import annotations

from datetime import UTC, datetime

from PIL import Image

from nightstand.models import Health
from nightstand.palette import PURE
from nightstand.screens import marker
from nightstand.screens.common import HEIGHT, WIDTH


def _blank() -> Image.Image:
    return Image.new("RGB", (WIDTH, HEIGHT), PURE["white"])


def _only_panel_colors(img: Image.Image) -> bool:
    colors = img.convert("RGB").getcolors(maxcolors=256)
    return colors is not None and {c for _, c in colors} <= set(PURE.values())


def test_apply_without_health_returns_image_unchanged():
    img = _blank()
    assert marker.apply(img, Health()) is img


def test_apply_draws_red_square_in_top_right_for_problems():
    out = marker.apply(_blank(), Health(problems=("office.local down",)))

    # A 14x14 red square sits 8 px in from the top-right corner.
    assert out.getpixel((WIDTH - 8 - 7, 8 + 7)) == PURE["red"]
    # The rest of the panel is untouched.
    assert out.getpixel((10, 10)) == PURE["white"]
    assert _only_panel_colors(out)


def test_apply_draws_since_text_beside_square_when_stale():
    stale = datetime(2026, 11, 6, 7, 5, tzinfo=UTC)
    out = marker.apply(_blank(), Health(stale_since=stale))

    # The square is still drawn.
    assert out.getpixel((WIDTH - 8 - 7, 8 + 7)) == PURE["red"]
    # "since 07:05" text appears to the left of the square: black pixels in the
    # top strip, left of where the square begins.
    square_left = WIDTH - 8 - 14
    black_left = 0
    for x in range(0, square_left):
        for y in range(0, 30):
            if out.getpixel((x, y)) == PURE["black"]:
                black_left += 1
    assert black_left > 0
    assert _only_panel_colors(out)
