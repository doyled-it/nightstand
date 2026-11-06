from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

from nightstand.models import ArtPiece
from nightstand.screens import art, placeholder


def sample_image() -> Image.Image:
    """A deterministic 1000x700 test image: a diagonal gradient with shapes.

    Non-square so the art screen's cover-crop has something to trim, and built
    in code so no artwork is downloaded or committed.
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


def test_render_art_matches_golden(assert_golden):
    piece = ArtPiece(path=Path("x.jpg"), title="Coastal Study", artist="Artist Name")

    assert_golden(art.render(piece, sample_image()), "art.png")


def test_render_art_is_panel_size(tmp_path):
    piece = ArtPiece(path=Path("x.jpg"), title="Title Only", artist=None)

    out = art.render(piece, sample_image())

    assert out.size == (800, 480)


def test_placeholder_matches_golden(assert_golden):
    assert_golden(placeholder.render("Add art to the share"), "placeholder.png")
