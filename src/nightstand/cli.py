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
from datetime import datetime
from pathlib import Path

from PIL import Image

from nightstand import __version__
from nightstand.art import ArtLibrary, load_piece_image
from nightstand.config import load_config
from nightstand.display import FileDisplay
from nightstand.screens import art, now_playing, placeholder
from nightstand.sources import snapserver

_EMPTY_ART_MESSAGE = "Add art to the share"
_NOTHING_PLAYING_MESSAGE = "Nothing playing"


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


def _run_preview(args: argparse.Namespace) -> int:
    if args.screen == "art":
        return _preview_art(args)
    if args.screen == "now-playing":
        return _preview_now_playing(args)
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
