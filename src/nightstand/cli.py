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
import os
import sys
from datetime import datetime
from pathlib import Path

import httpx
from PIL import Image

from nightstand import __version__
from nightstand.art import ArtLibrary, load_piece_image
from nightstand.config import Config, load_config
from nightstand.display import FileDisplay
from nightstand.screens import art, game, now_playing, placeholder, split, weather
from nightstand.sources import mlb, snapserver
from nightstand.sources import weather as weather_source

_EMPTY_ART_MESSAGE = "Add art to the share"
_NOTHING_PLAYING_MESSAGE = "Nothing playing"
_NO_GAME_MESSAGE = "No game today"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="nightstand",
        description="Bedside e-ink picture frame.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command")
    _add_preview(subparsers)

    args = parser.parse_args(argv)

    if args.command == "preview":
        return _run_preview(args)

    config = load_config()
    print(f"nightstand {__version__}: hub {config.hub.host}")
    return 0


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
        logos_dir = _preview_cache_dir(config) / "logos"
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

    if np is None:
        # With nothing playing there is no strip to draw; the plain weather
        # screen is the right fallback for a split preview.
        rendered = weather.render(forecast, at)
    else:
        cover = Image.open(args.cover) if args.cover else None
        rendered = split.render(forecast, np, cover, at)

    FileDisplay(args.out, panel=args.panel).show(rendered)
    return 0


def _preview_cache_dir(config: Config) -> Path:
    """The cache dir for a preview: the configured one if writable, else a
    per-user cache dir.

    The service runs as a user that owns ``paths.cache`` (``/var/lib/nightstand``
    by default). A preview on a dev machine has neither that directory nor root,
    so when the configured cache cannot be written it falls back to a per-user
    cache instead of failing when the logo cache tries to create it.
    """
    configured = config.paths.cache
    if _is_writable_dir(configured):
        return configured
    return _user_cache_dir()


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
