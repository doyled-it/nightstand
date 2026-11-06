from PIL import Image

from nightstand.palette import (
    PANEL,
    PURE,
    dither_to_panel,
    simulate_panel,
    to_six_colors,
)


def _colors(img):
    return {c for _, c in img.getcolors(1 << 20)}


def test_palettes_share_keys():
    assert set(PURE) == set(PANEL)
    assert set(PURE) == {"black", "white", "red", "yellow", "green", "blue"}


def test_dither_outputs_only_pure_colors():
    img = Image.linear_gradient("L").convert("RGB").resize((800, 480))
    assert _colors(dither_to_panel(img)) <= set(PURE.values())


def test_to_six_colors_outputs_only_pure_colors_without_dither():
    img = Image.linear_gradient("L").convert("RGB").resize((800, 480))
    out = to_six_colors(img)
    assert _colors(out) <= set(PURE.values())
    # A flat fill maps to exactly one pure color (no dithering spread).
    flat = Image.new("RGB", (64, 64), (250, 250, 250))
    assert _colors(to_six_colors(flat)) == {PURE["white"]}


def test_dither_actually_dithers():
    # Error diffusion must change the pixel layout versus a plain nearest-color
    # snap; otherwise no dithering happened.
    img = Image.linear_gradient("L").convert("RGB").resize((800, 480))
    assert dither_to_panel(img).tobytes() != to_six_colors(img).tobytes()


def test_simulate_panel_maps_pure_to_panel():
    src = Image.new("RGB", (32, 32), PURE["red"])
    assert _colors(simulate_panel(src)) == {PANEL["red"]}
