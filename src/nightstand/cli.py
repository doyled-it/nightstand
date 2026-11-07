"""Command-line entry point.

With no subcommand it resolves the config so an install can confirm the app is
wired up. ``preview`` renders a screen to an 800x480 PNG on the Mac, with no Pi
or panel attached. Each screen is its own ``preview`` subparser, so later tasks
add ``preview music``, ``preview game`` and the rest the same way ``preview
art`` is added here.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import httpx
from PIL import Image

from nightstand import __version__
from nightstand.app import AppState, LiveSources, run_forever, run_once
from nightstand.art import ArtLibrary, load_piece_image
from nightstand.config import Config, load_config
from nightstand.display import FileDisplay, InkyDisplay
from nightstand.models import ArtPiece, Health
from nightstand.screens import (
    art,
    game,
    marker,
    now_playing,
    placeholder,
    split,
    weather,
)
from nightstand.sources import mlb, snapserver
from nightstand.sources import weather as weather_source
from nightstand.sources.artsync import sync_art
from nightstand.sources.museums import fetch_museum

logger = logging.getLogger(__name__)

_EMPTY_ART_MESSAGE = "Add art to the share"
_NOTHING_PLAYING_MESSAGE = "Nothing playing"
_NO_GAME_MESSAGE = "No game today"
_NO_WEATHER_MESSAGE = "Weather unavailable"
_SELFTEST_MESSAGE = "Nightstand"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="nightstand",
        description="Bedside e-ink picture frame.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command")
    _add_preview(subparsers)
    _add_run(subparsers)
    _add_selftest(subparsers)
    _add_art(subparsers)

    args = parser.parse_args(argv)

    if args.command == "preview":
        return _run_preview(args)
    if args.command == "run":
        return _run(args)
    if args.command == "selftest":
        return _selftest(args)
    if args.command == "art":
        return _run_art(args)

    config = load_config()
    print(f"nightstand {__version__}: hub {config.hub.host}")
    return 0


def _add_run(subparsers: argparse._SubParsersAction) -> None:
    run = subparsers.add_parser("run", help="run the display loop")
    run.add_argument(
        "--display",
        choices=("inky", "file"),
        default="inky",
        help="where to push: the panel (default) or a PNG for development",
    )
    run.add_argument(
        "--out", type=Path, help="PNG path, required when --display file"
    )
    run.add_argument(
        "--once",
        action="store_true",
        help="render a single cycle and exit, instead of looping",
    )


def _add_selftest(subparsers: argparse._SubParsersAction) -> None:
    subparsers.add_parser(
        "selftest",
        help="render every screen from the bundled fixtures; non-zero on failure",
    )


def _add_art(subparsers: argparse._SubParsersAction) -> None:
    art_cmd = subparsers.add_parser("art", help="manage the local art collection")
    actions = art_cmd.add_subparsers(dest="action", required=True)

    actions.add_parser("sync", help="mirror the hub's art share to the local cache")

    fetch = actions.add_parser(
        "fetch", help="download public-domain works from a museum open-access API"
    )
    fetch.add_argument("source", choices=("met", "aic"), help="which museum API")
    fetch.add_argument("--query", required=True, help="search text")
    fetch.add_argument(
        "--limit", type=int, default=20, help="most works to fetch (default 20)"
    )
    fetch.add_argument(
        "--dest",
        type=Path,
        required=True,
        help="destination directory (e.g. the mounted art share); never the repo",
    )


def _add_preview(subparsers: argparse._SubParsersAction) -> None:
    preview = subparsers.add_parser("preview", help="render a screen to a PNG")
    screens = preview.add_subparsers(dest="screen", required=True)

    art_screen = screens.add_parser("art", help="the art screen")
    art_screen.add_argument("--art-dir", type=Path, required=True)
    art_screen.add_argument("--at", help="ISO timestamp to render for (default now)")
    art_screen.add_argument(
        "--panel",
        action="store_true",
        help="simulate the muted panel look",
    )
    art_screen.add_argument("-o", "--out", type=Path, required=True)

    now_playing_screen = screens.add_parser(
        "now-playing", help="the now-playing screen"
    )
    now_playing_screen.add_argument(
        "--fixture",
        type=Path,
        required=True,
        help="a recorded snapserver Server.GetStatus response",
    )
    now_playing_screen.add_argument(
        "--cover", type=Path, help="a local image to use as the cover art"
    )
    now_playing_screen.add_argument(
        "--panel",
        action="store_true",
        help="simulate the muted panel look",
    )
    now_playing_screen.add_argument("-o", "--out", type=Path, required=True)

    game_screen = screens.add_parser("game", help="the Padres game screen")
    game_screen.add_argument(
        "--fixture",
        type=Path,
        required=True,
        help="a recorded MLB feed/live response",
    )
    game_screen.add_argument(
        "--panel",
        action="store_true",
        help="simulate the muted panel look",
    )
    game_screen.add_argument("-o", "--out", type=Path, required=True)

    weather_screen = screens.add_parser("weather", help="the weather screen")
    weather_screen.add_argument(
        "--fixture",
        type=Path,
        required=True,
        help="a recorded Open-Meteo forecast response",
    )
    weather_screen.add_argument(
        "--at", help="ISO timestamp to render for (default now)"
    )
    weather_screen.add_argument(
        "--panel",
        action="store_true",
        help="simulate the muted panel look",
    )
    weather_screen.add_argument("-o", "--out", type=Path, required=True)

    split_screen = screens.add_parser("split", help="the weather and now-playing split")
    split_screen.add_argument(
        "--fixture",
        type=Path,
        required=True,
        help="a recorded Open-Meteo forecast response",
    )
    split_screen.add_argument(
        "--now-playing",
        type=Path,
        required=True,
        dest="now_playing",
        help="a recorded snapserver Server.GetStatus response",
    )
    split_screen.add_argument(
        "--cover", type=Path, help="a local image to use as the cover art"
    )
    split_screen.add_argument("--at", help="ISO timestamp to render for (default now)")
    split_screen.add_argument(
        "--panel",
        action="store_true",
        help="simulate the muted panel look",
    )
    split_screen.add_argument("-o", "--out", type=Path, required=True)


def _run_preview(args: argparse.Namespace) -> int:
    if args.screen == "art":
        return _preview_art(args)
    if args.screen == "now-playing":
        return _preview_now_playing(args)
    if args.screen == "game":
        return _preview_game(args)
    if args.screen == "weather":
        return _preview_weather(args)
    if args.screen == "split":
        return _preview_split(args)
    return 1


def _preview_art(args: argparse.Namespace) -> int:
    config = load_config()
    at = datetime.fromisoformat(args.at) if args.at else datetime.now()

    library = ArtLibrary(args.art_dir)
    piece = library.pick(at, config.art.rotate_minutes)

    image = load_piece_image(piece) if piece is not None else None
    if piece is None or image is None:
        rendered = placeholder.render(_EMPTY_ART_MESSAGE)
    else:
        rendered = art.render(piece, image)

    FileDisplay(args.out, panel=args.panel).show(rendered)
    return 0


def _preview_now_playing(args: argparse.Namespace) -> int:
    status = json.loads(args.fixture.read_text())
    result = status.get("result", status)
    np = snapserver.parse_now_playing(result)

    if np is None:
        rendered = placeholder.render(_NOTHING_PLAYING_MESSAGE)
    else:
        cover = Image.open(args.cover) if args.cover else None
        rendered = now_playing.render(np, cover)

    FileDisplay(args.out, panel=args.panel).show(rendered)
    return 0


def _preview_game(args: argparse.Namespace) -> int:
    config = load_config()
    feed = json.loads(args.fixture.read_text())
    state = mlb.parse_game(feed)

    if state is None:
        rendered = placeholder.render(_NO_GAME_MESSAGE)
    else:
        # Fetch real logos for a realistic preview; a logo that cannot be
        # fetched (offline, say) falls back to the team abbreviation.
        logos_dir = _resolve_cache_dir(config) / "logos"
        with httpx.Client() as client:
            cache = mlb.LogoCache(client, logos_dir)
            away_logo = cache.for_team(state.away.id)
            home_logo = cache.for_team(state.home.id)
        rendered = game.render(state, away_logo, home_logo)

    FileDisplay(args.out, panel=args.panel).show(rendered)
    return 0


def _preview_weather(args: argparse.Namespace) -> int:
    data = json.loads(args.fixture.read_text())
    at = datetime.fromisoformat(args.at) if args.at else datetime.now()
    forecast = weather_source.parse_forecast(data, at)

    if forecast is None:
        rendered = placeholder.render(_NO_WEATHER_MESSAGE)
    else:
        rendered = weather.render(forecast, at)
    FileDisplay(args.out, panel=args.panel).show(rendered)
    return 0


def _preview_split(args: argparse.Namespace) -> int:
    data = json.loads(args.fixture.read_text())
    at = datetime.fromisoformat(args.at) if args.at else datetime.now()
    forecast = weather_source.parse_forecast(data, at)

    status = json.loads(args.now_playing.read_text())
    result = status.get("result", status)
    np = snapserver.parse_now_playing(result)

    if forecast is None:
        rendered = placeholder.render(_NO_WEATHER_MESSAGE)
    elif np is None:
        # With nothing playing there is no strip to draw; the plain weather
        # screen is the right fallback for a split preview.
        rendered = weather.render(forecast, at)
    else:
        cover = Image.open(args.cover) if args.cover else None
        rendered = split.render(forecast, np, cover, at)

    FileDisplay(args.out, panel=args.panel).show(rendered)
    return 0


def _run(args: argparse.Namespace) -> int:
    config = load_config()
    display = _build_display(args)
    if display is None:
        return 2

    client = httpx.Client()
    try:
        sources = LiveSources(config, client, cache_dir=_resolve_cache_dir(config))
        if args.once:
            run_once(
                AppState(), config, sources, display, datetime.now().astimezone()
            )
            return 0
        run_forever(config, sources=sources, display=display)
    finally:
        client.close()
    return 0


def _run_art(args: argparse.Namespace) -> int:
    if args.action == "sync":
        return _art_sync(args)
    if args.action == "fetch":
        return _art_fetch(args)
    return 1


def _art_sync(args: argparse.Namespace) -> int:
    config = load_config()
    base_url = _art_base_url(config)
    dest = config.art.dir
    with httpx.Client() as client:
        result = sync_art(client, base_url, dest)
    print(
        f"art sync: +{result.added} added, {result.updated} updated, "
        f"{result.removed} removed"
    )
    return 0


def _art_fetch(args: argparse.Namespace) -> int:
    with httpx.Client() as client:
        result = fetch_museum(
            client, args.source, args.query, args.dest, args.limit
        )
    print(f"art fetch: {result.written} written, {result.skipped} already present")
    return 0


def _art_base_url(config: Config) -> str:
    """The hub's art listing URL, omitting the port when it is the default 80."""
    host = config.hub.host
    port = config.hub.http_port
    authority = host if port == 80 else f"{host}:{port}"
    return f"http://{authority}/art/"


def _build_display(args: argparse.Namespace):
    if args.display == "file":
        if args.out is None:
            print("run --display file requires --out", file=sys.stderr)
            return None
        return FileDisplay(args.out)
    return InkyDisplay()


def _selftest(args: argparse.Namespace) -> int:
    """Render every screen from the bundled fixtures; record pass/fail.

    This is the on-device check ``deploy/update.sh`` runs after an update. The
    result is written to ``paths.state/selftest.json`` for the updater to read
    before it decides whether to keep a new version or roll back.
    """
    config = load_config()
    try:
        _render_all_screens()
    except Exception as error:  # any failure to render is a failed selftest
        logger.exception("selftest failed")
        _record_selftest(config, "fail")
        print(f"selftest failed: {error}", file=sys.stderr)
        return 1
    _record_selftest(config, "pass")
    print("selftest passed")
    return 0


def _render_all_screens() -> None:
    """Render each screen once from fixtures, raising on any failure."""
    fixtures = _fixtures_dir()
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)

        _save(placeholder.render(_SELFTEST_MESSAGE), out / "placeholder.png")

        # Art is device-synced and not present here, so a solid image stands in
        # to exercise the dither and caption path.
        piece = ArtPiece(path=out / "art-src.png", title="Sample", artist="Artist")
        sample = Image.new("RGB", (400, 300), (0, 0, 255))
        _save(art.render(piece, sample), out / "art.png")

        status = json.loads(
            (fixtures / "snapserver" / "playing_airplay.json").read_text()
        )
        np = snapserver.parse_now_playing(status["result"])
        _save(now_playing.render(np, None), out / "now_playing.png")

        feed = json.loads((fixtures / "mlb" / "feed_final.json").read_text())
        state = mlb.parse_game(feed)
        # Logos need the network, which a selftest must not touch: None falls
        # back to the team abbreviation.
        _save(game.render(state, None, None), out / "game.png")

        data = json.loads((fixtures / "openmeteo" / "forecast.json").read_text())
        at = datetime.fromisoformat(data["daily"]["sunrise"][0])
        forecast = weather_source.parse_forecast(data, at)
        _save(weather.render(forecast, at), out / "weather.png")
        _save(split.render(forecast, np, None, at), out / "split.png")

        marked = marker.apply(
            weather.render(forecast, at),
            Health(problems=("selftest",), stale_since=at),
        )
        _save(marked, out / "marker.png")


def _save(image: Image.Image, path: Path) -> None:
    image.save(path)


def _record_selftest(config: Config, result: str) -> None:
    state_dir = _resolve_state_dir(config)
    state_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "result": result,
        "checked_at": datetime.now().astimezone().isoformat(),
    }
    (state_dir / "selftest.json").write_text(json.dumps(payload) + "\n")


def _fixtures_dir() -> Path:
    """The recorded fixtures under the repo checkout the app runs from."""
    return Path(__file__).resolve().parents[2] / "tests" / "fixtures"


def _resolve_cache_dir(config: Config) -> Path:
    """The cache dir: the configured one if writable, else a per-user dir.

    The service runs as a user that owns ``paths.cache`` (``/var/lib/nightstand``
    by default). A dev machine has neither that directory nor root, so when the
    configured cache cannot be written it falls back to a per-user cache instead
    of failing when a cache (logos, covers) tries to create it.
    """
    configured = config.paths.cache
    if _is_writable_dir(configured):
        return configured
    return _user_cache_dir()


def _resolve_state_dir(config: Config) -> Path:
    """The state dir: the configured one if writable, else a per-user dir.

    Mirrors ``_resolve_cache_dir`` so ``selftest`` can record its result on a
    dev machine, where ``paths.state`` is not writable.
    """
    configured = config.paths.state
    if _is_writable_dir(configured):
        return configured
    return _user_state_dir()


def _is_writable_dir(path: Path) -> bool:
    """Whether ``path`` exists-or-can-be-created and a file can be written in it."""
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write-test"
        probe.touch()
        probe.unlink()
        return True
    except OSError:
        return False


def _user_cache_dir() -> Path:
    """A per-user cache dir that needs no root, for previews on a dev machine."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "nightstand"
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".cache"
    return base / "nightstand"


def _user_state_dir() -> Path:
    """A per-user state dir that needs no root, for selftest on a dev machine."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "nightstand"
    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "state"
    return base / "nightstand"
