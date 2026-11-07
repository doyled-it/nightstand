from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

from nightstand.models import ArtPiece
from nightstand.palette import PURE
from nightstand.screens import art, placeholder
from nightstand.screens.common import HEIGHT, WIDTH


def _is_six_color(img: Image.Image) -> bool:
    colors = img.convert("RGB").getcolors(maxcolors=256)
    if colors is None:
        return False
    return {color for _, color in colors} <= set(PURE.values())


def sample_image() -> Image.Image:
    """A deterministic 1000x700 test image: a diagonal gradient with shapes.

    Non-square so the art screen's cover-crop scales it to fill the panel and
    crops the overflow, and built in code so no artwork is downloaded or
    committed.
    """
    img = Image.new("RGB", (1000, 700))
    draw = ImageDraw.Draw(img)
    for y in range(700):
        draw.line(
            [(0, y), (1000, y)],
            fill=(y * 255 // 700, 120, 200 - y * 200 // 700),
        )
    draw.ellipse([300, 150, 700, 550], fill=(255, 230, 40))
    draw.rectangle([120, 120, 260, 580], fill=(30, 60, 160))
    draw.polygon([(760, 500), (900, 560), (820, 660)], fill=(220, 40, 40))
    return img


def _edge_bands(img: Image.Image) -> list[list[tuple[int, int, int]]]:
    """The four one-pixel edge rows and columns, as lists of RGB pixels."""
    rgb = img.convert("RGB")
    w, h = rgb.size
    top = [rgb.getpixel((x, 0)) for x in range(w)]
    bottom = [rgb.getpixel((x, h - 1)) for x in range(w)]
    left = [rgb.getpixel((0, y)) for y in range(h)]
    right = [rgb.getpixel((w - 1, y)) for y in range(h)]
    return [top, bottom, left, right]


def _no_white_margin_bands(img: Image.Image) -> bool:
    """No edge row or column is entirely pure white: the art reaches every edge."""
    return all(
        any(px != PURE["white"] for px in band) for band in _edge_bands(img)
    )


def test_render_art_wide_fills_screen():
    piece = ArtPiece(path=Path("x.jpg"), title="Wide", artist=None)
    image = Image.new("RGB", (1000, 500), PURE["blue"])

    out = art.render(piece, image)

    assert out.size == (WIDTH, HEIGHT)
    assert _no_white_margin_bands(out)


def test_render_art_tall_fills_screen():
    piece = ArtPiece(path=Path("x.jpg"), title="Tall", artist=None)
    image = Image.new("RGB", (500, 1000), PURE["blue"])

    out = art.render(piece, image)

    assert out.size == (WIDTH, HEIGHT)
    assert _no_white_margin_bands(out)


def test_render_art_square_fills_screen():
    piece = ArtPiece(path=Path("x.jpg"), title="Square", artist=None)
    image = Image.new("RGB", (600, 600), PURE["blue"])

    out = art.render(piece, image)

    assert out.size == (WIDTH, HEIGHT)
    assert _no_white_margin_bands(out)


def test_render_art_matches_golden(assert_golden):
    piece = ArtPiece(path=Path("x.jpg"), title="Coastal Study", artist="Artist Name")

    assert_golden(art.render(piece, sample_image()), "art.png")


def test_render_art_is_panel_size(tmp_path):
    piece = ArtPiece(path=Path("x.jpg"), title="Title Only", artist=None)

    out = art.render(piece, sample_image())

    assert out.size == (800, 480)


def test_render_art_is_six_color():
    piece = ArtPiece(path=Path("x.jpg"), title="Coastal Study", artist="Artist Name")

    assert _is_six_color(art.render(piece, sample_image()))


def test_placeholder_matches_golden(assert_golden):
    assert_golden(placeholder.render("Add art to the share"), "placeholder.png")


def test_placeholder_is_six_color():
    assert _is_six_color(placeholder.render("Add art to the share"))
