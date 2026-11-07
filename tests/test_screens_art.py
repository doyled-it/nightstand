from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

from nightstand.models import ArtPiece
from nightstand.palette import PURE
from nightstand.screens import art, placeholder
from nightstand.screens.common import HEIGHT, WIDTH, contain_fit


def _is_six_color(img: Image.Image) -> bool:
    colors = img.convert("RGB").getcolors(maxcolors=256)
    if colors is None:
        return False
    return {color for _, color in colors} <= set(PURE.values())


def sample_image() -> Image.Image:
    """A deterministic 1000x700 test image: a diagonal gradient with shapes.

    Non-square so the art screen's contain-fit leaves white margins on two
    sides, and built in code so no artwork is downloaded or committed.
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


def _margins_are_pure_white(img: Image.Image, fitted: Image.Image) -> bool:
    """Every pixel outside the centered fitted region is pure white."""
    rgb = img.convert("RGB")
    left = (WIDTH - fitted.width) // 2
    top = (HEIGHT - fitted.height) // 2
    right = left + fitted.width
    bottom = top + fitted.height
    for x in range(WIDTH):
        for y in range(HEIGHT):
            if left <= x < right and top <= y < bottom:
                continue
            if rgb.getpixel((x, y)) != PURE["white"]:
                return False
    return True


def test_contain_fit_wide_image_fits_inside_canvas():
    fitted = contain_fit(Image.new("RGB", (1000, 500)), WIDTH, HEIGHT)
    assert fitted.width <= WIDTH and fitted.height <= HEIGHT
    # The limiting dimension (width) fills the canvas exactly.
    assert fitted.width == WIDTH
    # Aspect ratio preserved within rounding.
    assert abs(fitted.width / fitted.height - 1000 / 500) < 0.01


def test_contain_fit_tall_image_fits_inside_canvas():
    fitted = contain_fit(Image.new("RGB", (500, 1000)), WIDTH, HEIGHT)
    assert fitted.width <= WIDTH and fitted.height <= HEIGHT
    assert fitted.height == HEIGHT
    assert abs(fitted.width / fitted.height - 500 / 1000) < 0.01


def test_contain_fit_square_image_fits_inside_canvas():
    fitted = contain_fit(Image.new("RGB", (600, 600)), WIDTH, HEIGHT)
    assert fitted.width <= WIDTH and fitted.height <= HEIGHT
    assert fitted.height == HEIGHT
    assert fitted.width == fitted.height


def test_render_art_wide_has_pure_white_margins():
    piece = ArtPiece(path=Path("x.jpg"), title="Wide", artist=None)
    image = Image.new("RGB", (1000, 500), PURE["blue"])

    out = art.render(piece, image)
    fitted = contain_fit(image.convert("RGB"), WIDTH, HEIGHT)

    assert _margins_are_pure_white(out, fitted)


def test_render_art_tall_has_pure_white_margins():
    piece = ArtPiece(path=Path("x.jpg"), title="Tall", artist=None)
    image = Image.new("RGB", (500, 1000), PURE["blue"])

    out = art.render(piece, image)
    fitted = contain_fit(image.convert("RGB"), WIDTH, HEIGHT)

    assert _margins_are_pure_white(out, fitted)


def test_render_art_square_has_pure_white_margins():
    piece = ArtPiece(path=Path("x.jpg"), title="Square", artist=None)
    image = Image.new("RGB", (600, 600), PURE["blue"])

    out = art.render(piece, image)
    fitted = contain_fit(image.convert("RGB"), WIDTH, HEIGHT)

    assert _margins_are_pure_white(out, fitted)


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
