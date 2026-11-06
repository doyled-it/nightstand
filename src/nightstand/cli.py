"""Command-line entry point.

A thin shell for now: it resolves the config so an install can confirm the app
is wired up. Screen rendering and the preview command arrive in later tasks.
"""

from __future__ import annotations

import argparse

from nightstand import __version__
from nightstand.config import load_config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="nightstand",
        description="Bedside e-ink picture frame.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.parse_args(argv)

    config = load_config()
    print(f"nightstand {__version__}: hub {config.hub.host}")
    return 0
