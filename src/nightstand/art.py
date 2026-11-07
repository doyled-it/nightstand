"""The local art library: list the pieces, caption them, and pick one to show.

Images live in a folder synced from the hub's art share. Each image may have a
sidecar caption file named ``<image file>.txt`` beside it (line 1 the title,
line 2 the artist). Without one, the title falls back to the filename.

``pick`` is deterministic: it maps the clock to a time slot and returns the same
piece for the whole slot, stepping to the next piece when the slot changes. The
order is reshuffled each day so the sequence varies day to day.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from random import Random

from PIL import Image, UnidentifiedImageError

from nightstand.models import ArtPiece

logger = logging.getLogger(__name__)

_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
_MINUTES_PER_DAY = 60 * 24


class ArtLibrary:
    """The art folder: the pieces in it and the rotation across them."""

    def __init__(self, dir: Path) -> None:
        self.dir = Path(dir)

    def pieces(self) -> list[ArtPiece]:
        """Every image in the folder, captioned, sorted by filename."""
        if not self.dir.is_dir():
            return []
        files = sorted(
            entry
            for entry in self.dir.iterdir()
            if entry.is_file() and entry.suffix.lower() in _EXTENSIONS
        )
        return [_piece_for(path) for path in files]

    def pick(self, now: datetime, rotate_minutes: int) -> ArtPiece | None:
        """The piece to show at ``now``, stable within each rotation slot."""
        pieces = self.pieces()
        if not pieces:
            return None
        epoch_minutes = int(now.timestamp()) // 60
        slot = art_slot(now, rotate_minutes)
        day = epoch_minutes // _MINUTES_PER_DAY
        order = list(pieces)
        Random(day).shuffle(order)
        return order[slot % len(order)]


def art_slot(now: datetime, rotate_minutes: int) -> int:
    """The rotation slot ``now`` falls in.

    Buckets absolute epoch minutes, so it is independent of any timezone or DST
    shift. ``pick`` and the rules engine's art redraw key share this helper so
    the key always changes in step with the piece ``pick`` returns.
    """
    return int(now.timestamp()) // 60 // rotate_minutes


def load_piece_image(piece: ArtPiece) -> Image.Image | None:
    """Open a piece's image as RGB, or ``None`` if the file cannot be read."""
    try:
        with Image.open(piece.path) as img:
            return img.convert("RGB")
    except (OSError, UnidentifiedImageError):
        logger.warning("could not read art image %s", piece.path)
        return None


def _piece_for(path: Path) -> ArtPiece:
    title, artist = _caption(path)
    return ArtPiece(path=path, title=title, artist=artist)


def _caption(path: Path) -> tuple[str, str | None]:
    sidecar = path.with_name(path.name + ".txt")
    if sidecar.is_file():
        lines = [line.strip() for line in sidecar.read_text().splitlines()]
        lines = [line for line in lines if line]
        if lines:
            title = lines[0]
            artist = lines[1] if len(lines) > 1 else None
            return title, artist
    return _title_from_filename(path), None


def _title_from_filename(path: Path) -> str:
    return path.stem.replace("_", " ").replace("-", " ")
