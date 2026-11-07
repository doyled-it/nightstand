from __future__ import annotations

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


def test_apply_draws_no_text_beside_square():
    # Staleness no longer draws a "since HH:MM" label: only the square appears,
    # so the strip to the left of the square stays blank.
    out = marker.apply(_blank(), Health(problems=("office.local down",)))

    square_left = WIDTH - 8 - 14
    for x in range(0, square_left):
        for y in range(0, 30):
            assert out.getpixel((x, y)) == PURE["white"]
