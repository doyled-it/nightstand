from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageChops

from nightstand.cli import main
from nightstand.palette import PURE
from nightstand.screens import placeholder

FIXTURES = Path(__file__).parent / "fixtures" / "snapserver"
_EMPTY_ART_MESSAGE = "Add art to the share"


def _identical(a: Image.Image, b: Image.Image) -> bool:
    return ImageChops.difference(a.convert("RGB"), b.convert("RGB")).getbbox() is None


def test_preview_art_on_empty_dir_renders_placeholder(tmp_path):
    out = tmp_path / "art.png"

    code = main(["preview", "art", "--art-dir", str(tmp_path), "-o", str(out)])

    assert code == 0
    rendered = Image.open(out)
    assert _identical(rendered, placeholder.render(_EMPTY_ART_MESSAGE))


def test_preview_now_playing_renders_from_fixture(tmp_path):
    out = tmp_path / "now.png"

    code = main(
        [
            "preview",
            "now-playing",
            "--fixture",
            str(FIXTURES / "playing_airplay.json"),
            "-o",
            str(out),
        ]
    )

    assert code == 0
    rendered = Image.open(out).convert("RGB")
    assert rendered.size == (800, 480)
    colors = rendered.getcolors(maxcolors=256)
    assert colors is not None
    assert {color for _, color in colors} <= set(PURE.values())
