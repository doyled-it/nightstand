"""The art screen applies a published crop box; detail_crop is only the fallback."""

from __future__ import annotations

import io
import logging
from dataclasses import replace
from pathlib import Path

import pytest
from PIL import Image, ImageChops, ImageDraw, ImageStat

from nightstand.art import load_piece_image
from nightstand.models import ArtPiece
from nightstand.palette import dither_to_panel
from nightstand.screens import art
from nightstand.screens.common import HEIGHT, WIDTH, crop_to_panel, detail_crop

# Generated pictures, copied from the hub's tests/python/test_framecrop.py so the
# two repos pin the same golden windows. (size, scaled size, window offset)
GOLDEN = {
    "landscape": ((1600, 900), (853, 480), (53, 0)),
    "wide": ((2400, 800), (1440, 480), (0, 0)),
    "portrait": ((600, 1000), (800, 1333), (0, 853)),
    "near_5_3": ((1000, 590), (814, 480), (7, 0)),
    "uniform": ((900, 900), (800, 800), (0, 157)),
    "square_busy_top": ((1000, 1000), (800, 800), (0, 0)),
}


def _noise_block(draw, box, seed):
    x0, y0, x1, y1 = box
    for cy in range(y0, y1, 8):
        for cx in range(x0, x1, 8):
            v = ((cx * 73856093) ^ (cy * 19349663) ^ (seed * 83492791)) & 0xFF
            draw.rectangle(
                (cx, cy, cx + 7, cy + 7), fill=(v, (v * 7) & 0xFF, (v * 13) & 0xFF)
            )


def _picture(name):
    w, h = GOLDEN[name][0]
    image = Image.new("RGB", (w, h), (200, 200, 200))
    draw = ImageDraw.Draw(image)
    if name == "landscape":
        _noise_block(draw, (1100, 200, 1500, 700), 1)
    elif name == "wide":
        _noise_block(draw, (100, 100, 500, 700), 2)
    elif name == "portrait":
        _noise_block(draw, (100, 700, 500, 950), 3)
    elif name == "near_5_3":
        _noise_block(draw, (700, 100, 950, 500), 4)
    elif name == "square_busy_top":
        _noise_block(draw, (200, 40, 800, 300), 5)
    return image


def _piece(**kw) -> ArtPiece:
    return ArtPiece(path=Path("x.jpg"), title="t", id="a.jpg", **kw)


def _mean_abs_diff(a: Image.Image, b: Image.Image) -> float:
    diff = ImageChops.difference(a.convert("RGB"), b.convert("RGB"))
    return sum(ImageStat.Stat(diff).mean) / 3


def _quadrants() -> Image.Image:
    """2000x1200, four flat colored quadrants, easy to tell which one was cut."""
    image = Image.new("RGB", (2000, 1200))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 999, 599), fill=(255, 0, 0))
    draw.rectangle((1000, 0, 1999, 599), fill=(0, 255, 0))
    draw.rectangle((0, 600, 999, 1199), fill=(0, 0, 255))
    draw.rectangle((1000, 600, 1999, 1199), fill=(255, 255, 0))
    return image


def test_a_saved_crop_gives_exactly_that_box():
    image = _picture("landscape")
    box = (0.25, 0.1, 0.5, 0.5 * 1600 / 900 / (800 / 480) * 1.0)
    out = crop_to_panel(image, box)
    left, top = round(0.25 * 1600), round(0.1 * 900)
    right = round((0.25 + 0.5) * 1600)
    bottom = round((box[1] + box[3]) * 900)
    expected = image.crop((left, top, right, bottom)).resize(
        (WIDTH, HEIGHT), Image.Resampling.LANCZOS
    )
    assert out.size == (WIDTH, HEIGHT)
    assert out.tobytes() == expected.tobytes()


def test_render_with_a_crop_cuts_the_box_then_dithers():
    image = _quadrants()
    piece = _piece(crop=(0.5, 0.0, 0.5, 0.5 * 2000 / 1200 / (800 / 480) * 1.0))
    # box covers the top-right quadrant: green, 1000x600
    out = art.render(piece, image).convert("RGB")
    assert out.size == (WIDTH, HEIGHT)
    assert {c for _, c in out.getcolors(maxcolors=100)} == {(0, 255, 0)}
    assert out.getpixel((400, 240)) == (0, 255, 0)


def test_saved_crop_wins_over_auto_crop():
    image = _quadrants()
    h = 0.5 * 2000 / 1200 / (800 / 480)
    piece = _piece(crop=(0.0, 0.5, 0.5, h), auto_crop=(0.5, 0.0, 0.5, h))
    out = art.render(piece, image).convert("RGB")
    assert out.getpixel((400, 240)) == (0, 0, 255)


def test_auto_crop_is_used_when_there_is_no_crop():
    image = _quadrants()
    h = 0.5 * 2000 / 1200 / (800 / 480)
    out = art.render(_piece(auto_crop=(0.5, 0.0, 0.5, h)), image).convert("RGB")
    assert out.getpixel((400, 240)) == (0, 255, 0)


@pytest.mark.parametrize("name", sorted(GOLDEN))
def test_hub_auto_crop_reads_like_detail_crop(name):
    """The hub's auto_crop for a golden picture shows what detail_crop showed."""
    (width, height), (sw, sh), (left, top) = GOLDEN[name]
    image = _picture(name)
    box = (left / sw, top / sh, 800 / sw, 480 / sh)
    ours = crop_to_panel(image, box)
    before = detail_crop(image.convert("RGB"), WIDTH, HEIGHT)
    assert ours.size == before.size
    assert _mean_abs_diff(ours, before) < 6.0
    # And the same window: the busy block sits in the same place.
    assert _mean_abs_diff(ours.resize((80, 48)), before.resize((80, 48))) < 6.0


def test_without_any_box_the_render_is_exactly_the_old_detail_crop():
    image = _picture("landscape")
    expected = dither_to_panel(detail_crop(image.convert("RGB"), WIDTH, HEIGHT))
    assert art.render(_piece(), image).tobytes() == expected.tobytes()


@pytest.mark.parametrize(
    "bad",
    [
        (1.2, 0.0, 0.5, 0.5),
        (0.0, 0.0, 0.0, 0.5),
        (0.0, 0.0, 0.5, 0.0),
        (float("nan"), 0.0, 0.5, 0.5),
        (0.8, 0.0, 0.5, 0.5),
        "box",
        (1, 2, 3),
        5,
        None,
    ],
)
def test_a_bad_box_falls_back_to_detail_crop_without_raising(bad):
    image = _picture("landscape")
    piece = replace(_piece(), crop=bad)
    expected = dither_to_panel(detail_crop(image.convert("RGB"), WIDTH, HEIGHT))
    assert art.render(piece, image).tobytes() == expected.tobytes()


def test_a_bad_crop_falls_back_to_a_good_auto_crop():
    image = _quadrants()
    h = 0.5 * 2000 / 1200 / (800 / 480)
    piece = _piece(crop=(2.0, 0.0, 0.5, h), auto_crop=(0.5, 0.0, 0.5, h))
    assert art.render(piece, image).convert("RGB").getpixel((400, 240)) == (0, 255, 0)


def test_a_failing_crop_is_logged_once_and_falls_back(monkeypatch, caplog):
    def boom(image, box):
        raise ValueError("boom")

    monkeypatch.setattr(art, "crop_to_panel", boom)
    image = _picture("landscape")
    piece = _piece(crop=(0.0, 0.0, 0.5, 0.5))
    with caplog.at_level(logging.WARNING):
        art.render(piece, image)
        art.render(piece, image)
    assert len([r for r in caplog.records if "crop" in r.getMessage()]) == 1


def test_an_exif_rotated_file_crops_the_upright_grid(tmp_path):
    # Stored 1200x2000 with orientation 6 (rotate 90 CW to view): upright 2000x1200
    # is the quadrants picture. Cut the top-right upright quadrant (green).
    upright = _quadrants()
    stored = upright.rotate(90, expand=True)  # CCW 90; undone by orientation 6
    exif = Image.Exif()
    exif[0x0112] = 6
    path = tmp_path / "p2.jpg"
    buf = io.BytesIO()
    stored.save(buf, format="JPEG", quality=95, exif=exif.tobytes())
    path.write_bytes(buf.getvalue())

    piece = ArtPiece(
        path=path,
        title="t",
        id="p2.jpg",
        crop=(0.5, 0.0, 0.5, 0.5 * 2000 / 1200 / (800 / 480)),
    )
    loaded = load_piece_image(piece)
    assert loaded.size == (2000, 1200)
    out = art.render(piece, loaded).convert("RGB")
    assert out.getpixel((400, 240)) == (0, 255, 0)
