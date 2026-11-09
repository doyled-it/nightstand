from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageChops

from nightstand.cli import main
from nightstand.models import Health
from nightstand.palette import PURE
from nightstand.screens import placeholder

FIXTURES = Path(__file__).parent / "fixtures" / "nowplaying"
MLB_FIXTURES = Path(__file__).parent / "fixtures" / "mlb"
OPENMETEO_FIXTURES = Path(__file__).parent / "fixtures" / "openmeteo"
_EMPTY_ART_MESSAGE = "Add art to the share"
_NO_GAME_MESSAGE = "No game today"


def _only_panel_colors(rendered: Image.Image) -> bool:
    colors = rendered.getcolors(maxcolors=256)
    return colors is not None and {color for _, color in colors} <= set(PURE.values())


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
            str(FIXTURES / "preview_podcast.json"),
            "--at",
            "2026-11-08T23:00:05-08:00",
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


class _FakeSources:
    """A ``Sources`` with nothing playing, for the run CLI.

    A single cycle renders the placeholder and writes it: nothing is playing, so
    the now-playing settle (which would hold a fresh track off a one-shot cycle)
    does not apply here.
    """

    def now_playing(self):
        return None

    def cover(self, np):
        return None

    def game(self):
        return None

    def logo(self, team_id):
        return None

    def forecast(self):
        return None

    def art(self, now):
        return None

    def health(self):
        return Health()


def test_run_once_file_display_writes_png(tmp_path, monkeypatch):
    # The quiet window [00:00, 00:00) never matches, so a single cycle always
    # pushes regardless of the wall clock. The fake Sources avoids all I/O.
    config = tmp_path / "local.toml"
    config.write_text(
        '[schedule]\nquiet_start = "00:00"\nquiet_end = "00:00"\n'
        '[location]\ntimezone = "UTC"\n'
    )
    monkeypatch.setenv("NIGHTSTAND_CONFIG", str(config))
    monkeypatch.setattr(
        "nightstand.cli.LiveSources", lambda *a, **k: _FakeSources()
    )
    out = tmp_path / "frame.png"

    code = main(["run", "--once", "--display", "file", "--out", str(out)])

    assert code == 0
    rendered = Image.open(out).convert("RGB")
    assert rendered.size == (800, 480)


def test_selftest_renders_all_screens_and_passes(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    config = tmp_path / "local.toml"
    config.write_text(f'[paths]\nstate = "{state_dir}"\n')
    monkeypatch.setenv("NIGHTSTAND_CONFIG", str(config))

    code = main(["selftest"])

    assert code == 0
    # update.sh uses the exit status directly; selftest writes nothing to disk.
    assert not (state_dir / "selftest.json").exists()


def test_preview_weather_renders_from_fixture(tmp_path):
    out = tmp_path / "weather.png"

    code = main(
        [
            "preview",
            "weather",
            "--fixture",
            str(OPENMETEO_FIXTURES / "forecast.json"),
            "--at",
            "2026-11-06T06:30",
            "-o",
            str(out),
        ]
    )

    assert code == 0
    rendered = Image.open(out).convert("RGB")
    assert rendered.size == (800, 480)
    assert _only_panel_colors(rendered)


def test_preview_split_renders_from_fixtures(tmp_path):
    out = tmp_path / "split.png"

    code = main(
        [
            "preview",
            "split",
            "--fixture",
            str(OPENMETEO_FIXTURES / "forecast.json"),
            "--now-playing",
            str(FIXTURES / "spec_example.json"),
            "--at",
            "2026-11-06T06:30",
            "-o",
            str(out),
        ]
    )

    assert code == 0
    rendered = Image.open(out).convert("RGB")
    assert rendered.size == (800, 480)
    assert _only_panel_colors(rendered)


def test_preview_game_succeeds_when_configured_cache_dir_is_unwritable(
    tmp_path, monkeypatch
):
    # The service cache default (/var/lib/nightstand) is not writable on a dev
    # Mac with no root, so a preview must fall back to a per-user cache dir
    # instead of failing. A regular file stands in for the unwritable location:
    # the configured cache dir cannot be created under it, even as root.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    config = tmp_path / "local.toml"
    config.write_text(f'[paths]\ncache = "{blocker / "cache"}"\n')
    monkeypatch.setenv("NIGHTSTAND_CONFIG", str(config))

    # Point the per-user fallback at a tmp dir pre-seeded with logos, so the
    # render needs no network once the fallback is chosen.
    fallback = tmp_path / "user-cache"
    monkeypatch.setattr("nightstand.cli._user_cache_dir", lambda: fallback)
    _seed_logo(fallback / "logos" / "135.png")
    _seed_logo(fallback / "logos" / "134.png")
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


class _RecordingInky:
    """A fake ``InkyDisplay`` that records its saturation and the image shown."""

    instances: list[_RecordingInky] = []

    def __init__(self, saturation=None):
        self.saturation = saturation
        self.shown: Image.Image | None = None
        _RecordingInky.instances.append(self)

    def show(self, img: Image.Image) -> None:
        self.shown = img


def test_show_pushes_png_to_panel(tmp_path, monkeypatch):
    _RecordingInky.instances = []
    monkeypatch.setattr("nightstand.cli.InkyDisplay", _RecordingInky)
    png = tmp_path / "frame.png"
    Image.new("RGB", (800, 480), (0, 0, 255)).save(png)

    code = main(["show", str(png)])

    assert code == 0
    assert len(_RecordingInky.instances) == 1
    inky = _RecordingInky.instances[0]
    assert inky.saturation is None
    assert inky.shown is not None
    assert inky.shown.size == (800, 480)


def test_show_passes_saturation(tmp_path, monkeypatch):
    _RecordingInky.instances = []
    monkeypatch.setattr("nightstand.cli.InkyDisplay", _RecordingInky)
    png = tmp_path / "frame.png"
    Image.new("RGB", (800, 480), (255, 0, 0)).save(png)

    code = main(["show", str(png), "--saturation", "0.8"])

    assert code == 0
    assert _RecordingInky.instances[0].saturation == 0.8


def test_art_sync_uses_config_hub_and_art_dir(tmp_path, monkeypatch):
    captured = {}

    def fake_sync_art(client, base_url, dest):
        captured["base_url"] = base_url
        captured["dest"] = dest
        from nightstand.sources.artsync import SyncResult

        return SyncResult(added=2, updated=1, removed=0)

    monkeypatch.setattr("nightstand.cli.sync_art", fake_sync_art)
    config = tmp_path / "local.toml"
    art_dir = tmp_path / "art"
    config.write_text(
        f'[hub]\nhost = "hub.example"\n[art]\ndir = "{art_dir}"\n'
    )
    monkeypatch.setenv("NIGHTSTAND_CONFIG", str(config))

    code = main(["art", "sync"])

    assert code == 0
    assert captured["base_url"] == "http://hub.example/art/"
    assert captured["dest"] == art_dir


def test_art_sync_includes_non_default_http_port(tmp_path, monkeypatch):
    captured = {}

    def fake_sync_art(client, base_url, dest):
        captured["base_url"] = base_url
        from nightstand.sources.artsync import SyncResult

        return SyncResult()

    monkeypatch.setattr("nightstand.cli.sync_art", fake_sync_art)
    config = tmp_path / "local.toml"
    config.write_text('[hub]\nhost = "hub.example"\nhttp_port = 8080\n')
    monkeypatch.setenv("NIGHTSTAND_CONFIG", str(config))

    code = main(["art", "sync"])

    assert code == 0
    assert captured["base_url"] == "http://hub.example:8080/art/"


def test_art_fetch_passes_arguments_through(tmp_path, monkeypatch):
    captured = {}

    def fake_fetch(client, source, query, dest, limit, min_aspect):
        captured.update(
            source=source,
            query=query,
            dest=dest,
            limit=limit,
            min_aspect=min_aspect,
        )
        from nightstand.sources.museums import FetchResult

        return FetchResult(written=3, skipped=1)

    monkeypatch.setattr("nightstand.cli.fetch_museum", fake_fetch)
    dest = tmp_path / "share"

    code = main(
        [
            "art", "fetch", "met",
            "--query", "sunset",
            "--limit", "5",
            "--dest", str(dest),
        ]
    )

    assert code == 0
    assert captured["source"] == "met"
    assert captured["query"] == "sunset"
    assert captured["dest"] == dest
    assert captured["limit"] == 5
    assert captured["min_aspect"] == 1.3  # the default


def test_art_fetch_min_aspect_override(tmp_path, monkeypatch):
    captured = {}

    def fake_fetch(client, source, query, dest, limit, min_aspect):
        captured.update(min_aspect=min_aspect)
        from nightstand.sources.museums import FetchResult

        return FetchResult()

    monkeypatch.setattr("nightstand.cli.fetch_museum", fake_fetch)

    code = main(
        [
            "art", "fetch", "aic",
            "--query", "sea",
            "--dest", str(tmp_path / "share"),
            "--min-aspect", "1.6",
        ]
    )

    assert code == 0
    assert captured["min_aspect"] == 1.6


def test_art_fetch_reports_shape_skips(tmp_path, monkeypatch, capsys):
    def fake_fetch(client, source, query, dest, limit, min_aspect):
        from nightstand.sources.museums import FetchResult

        return FetchResult(found=4, kept=3, written=1, skipped=0, skipped_shape=2)

    monkeypatch.setattr("nightstand.cli.fetch_museum", fake_fetch)

    main(["art", "fetch", "met", "--query", "x", "--dest", str(tmp_path / "s")])

    out = capsys.readouterr().out
    assert "2 skipped (shape)" in out


