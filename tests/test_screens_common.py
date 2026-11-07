from __future__ import annotations

from PIL import Image

from nightstand.screens.common import HEIGHT, WIDTH, cover_crop, detail_crop


def _white_pixels(img: Image.Image) -> int:
    rgb = img.convert("RGB")
    return sum(
        count
        for count, color in rgb.getcolors(maxcolors=1 << 24)
        if color == (255, 255, 255)
    )


def _mean(img: Image.Image) -> float:
    data = img.convert("L").tobytes()
    return sum(data) / len(data)


def test_detail_crop_output_is_exact_panel_size():
    image = Image.new("RGB", (1234, 567), (90, 120, 150))

    out = detail_crop(image, WIDTH, HEIGHT)

    assert out.size == (WIDTH, HEIGHT)


def test_detail_crop_window_covers_the_detailed_side():
    # A wide black image with all the detail (a white bar) crammed on the left.
    # A centered cover-crop would discard it; the detail-aware crop must slide
    # its window left to keep the busy region.
    image = Image.new("RGB", (1600, 480), (0, 0, 0))
    for x in range(40):
        for y in range(480):
            image.putpixel((x, y), (255, 255, 255))

    out = detail_crop(image, WIDTH, HEIGHT)
    centered = cover_crop(image, WIDTH, HEIGHT)

    assert out.size == (WIDTH, HEIGHT)
    assert _white_pixels(out) > 10000
    assert _white_pixels(centered) == 0


def test_detail_crop_uniform_detail_is_centered():
    # A smooth horizontal gradient has the same edge energy in every column, so
    # no window is busier than another and the crop stays centered. The centered
    # window's brightness is the gradient's mid value.
    image = Image.new("RGB", (1600, 480))
    for x in range(1600):
        v = x * 255 // 1599
        for y in range(480):
            image.putpixel((x, y), (v, v, v))

    out = detail_crop(image, WIDTH, HEIGHT)

    assert out.size == (WIDTH, HEIGHT)
    assert abs(_mean(out) - 128) < 12


def test_detail_crop_near_target_aspect_is_centered():
    # Already within tolerance of 5:3, so the crop is centered (equal to the
    # plain cover-crop) rather than slid toward the detail.
    image = Image.new("RGB", (820, 480), (0, 0, 0))
    for x in range(40):
        for y in range(480):
            image.putpixel((x, y), (255, 255, 255))

    out = detail_crop(image, WIDTH, HEIGHT)
    centered = cover_crop(image, WIDTH, HEIGHT)

    assert out.convert("RGB").tobytes() == centered.convert("RGB").tobytes()
