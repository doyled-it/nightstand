from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageChops

from nightstand.cli import main
from nightstand.palette import PURE
from nightstand.screens import placeholder

FIXTURES = Path(__file__).parent / "fixtures" / "snapserver"
MLB_FIXTURES = Path(__file__).parent / "fixtures" / "mlb"
_EMPTY_ART_MESSAGE = "Add art to the share"
_NO_GAME_MESSAGE = "No game today"


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


def test_preview_game_on_preview_feed_renders_placeholder(tmp_path):
    # A game that has not started parses to None, so there is nothing to show.
    out = tmp_path / "game.png"

    code = main(
        [
            "preview",
            "game",
            "--fixture",
            str(MLB_FIXTURES / "feed_preview.json"),
            "-o",
            str(out),
        ]
    )

    assert code == 0
    rendered = Image.open(out)
    assert _identical(rendered, placeholder.render(_NO_GAME_MESSAGE))


def _seed_logo(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (150, 150), (0, 0, 255)).save(path)


def test_preview_game_renders_from_fixture(tmp_path, monkeypatch):
    # Point the cache at a tmp dir pre-seeded with logos, so the render needs no
    # network: the logo cache finds both teams' logos on disk.
    config = tmp_path / "local.toml"
    cache = tmp_path / "cache"
    config.write_text(f'[paths]\ncache = "{cache}"\n')
    monkeypatch.setenv("NIGHTSTAND_CONFIG", str(config))
    _seed_logo(cache / "logos" / "135.png")
    _seed_logo(cache / "logos" / "134.png")
    out = tmp_path / "game.png"

    code = main(
        [
            "preview",
            "game",
            "--fixture",
            str(MLB_FIXTURES / "feed_scoring_play.json"),
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
