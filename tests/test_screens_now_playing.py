from __future__ import annotations

from PIL import Image, ImageChops, ImageDraw

from nightstand.models import NowPlaying
from nightstand.palette import PURE
from nightstand.screens import now_playing


def _is_six_color(img: Image.Image) -> bool:
    colors = img.convert("RGB").getcolors(maxcolors=256)
    if colors is None:
        return False
    return {color for _, color in colors} <= set(PURE.values())


def sample_cover() -> Image.Image:
    """A deterministic non-square cover image, built in code (nothing downloaded)."""
    img = Image.new("RGB", (640, 480))
    draw = ImageDraw.Draw(img)
    for y in range(480):
        draw.line([(0, y), (640, y)], fill=(40, y * 255 // 480, 180 - y * 120 // 480))
    draw.ellipse([180, 120, 460, 400], fill=(240, 210, 60))
    draw.rectangle([60, 60, 160, 420], fill=(200, 40, 50))
    return img


def a_now_playing(title: str = "Tidal Drift") -> NowPlaying:
    return NowPlaying(
        title=title,
        artist="Nova Reef, The Saltwater Choir",
        album="Blue Hour",
        art_url="http://hub.local/art/blue-hour.jpg",
        rooms=("Living room", "Bedroom"),
    )


def test_render_with_cover_matches_golden(assert_golden):
    rendered = now_playing.render(a_now_playing(), sample_cover())
    assert_golden(rendered, "now_playing_cover.png")


def test_render_without_cover_matches_golden(assert_golden):
    rendered = now_playing.render(a_now_playing(), None)
    assert_golden(rendered, "now_playing_text.png")


def test_render_is_panel_size():
    assert now_playing.render(a_now_playing(), sample_cover()).size == (800, 480)


def test_render_with_cover_is_six_color():
    assert _is_six_color(now_playing.render(a_now_playing(), sample_cover()))


def test_render_without_cover_is_six_color():
    assert _is_six_color(now_playing.render(a_now_playing(), None))


def test_long_title_stays_within_text_column():
    long_title = "Movement " * 40  # 360 characters
    np = a_now_playing(title=long_title.strip())

    rendered = now_playing.render(np, sample_cover()).convert("RGB")

    # The cover fills x < 480; everything the text column draws must stay left of
    # x = 794. Measure the non-white pixels in the region to the right of the cover.
    column = rendered.crop((480, 0, 800, 480))
    white = Image.new("RGB", column.size, PURE["white"])
    bbox = ImageChops.difference(column, white).getbbox()
    assert bbox is not None, "expected the text column to draw something"
    rightmost = 480 + bbox[2] - 1
    assert rightmost <= 794, f"text reached x={rightmost}, past the x=794 limit"
