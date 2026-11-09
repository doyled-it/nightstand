"""The local art library: list the pieces, caption them, pick the eligible ones.

Images live in a folder synced from the hub's library. Top-level files are the
built-in "Collection"; each subdirectory one level down is a user folder. Each
image may have a sidecar caption file named ``<image file>.txt`` beside it (line
1 the title, line 2 the artist, optional line 3 the museum object page as an
https URL). Without one, the title falls back to the filename.

``eligible`` lists the pieces that may rotate (landscape enough to fill the
panel); the live loop drives the rotation across them through the stateful
``Rotation`` machine in ``rotation.py``. ``pick`` is the older stateless picker,
kept for the ``preview art`` dev command: it maps the clock to a daily-shuffled
slot and returns the same piece for the whole slot.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from random import Random
from urllib.parse import urlparse

from PIL import Image, UnidentifiedImageError

from nightstand.models import ArtPiece

logger = logging.getLogger(__name__)

_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
_MINUTES_PER_DAY = 60 * 24


class ArtLibrary:
    """The art folder: the pieces in it, their captions, and which may rotate."""

    def __init__(self, dir: Path) -> None:
        self.dir = Path(dir)
        # Cache of each image's aspect, keyed by path and invalidated when the
        # file's mtime or size changes, so pick does not decode every image on
        # every cycle just to measure its shape.
        self._aspect_cache: dict[Path, tuple[float, int, float]] = {}

    def pieces(self) -> list[ArtPiece]:
        """Every image in the library, captioned, with its id, sorted by id.

        Top-level images form the built-in "Collection"; each subdirectory one
        level down is a user folder. Deeper nesting, ``.trash``, and other hidden
        folders are ignored, matching the hub's library layout. Each piece's
        ``id`` is its path relative to the art directory (``met-1.jpg`` or
        ``Photos/beach.jpg``).
        """
        if not self.dir.is_dir():
            return []
        paths: list[Path] = []
        for entry in self.dir.iterdir():
            if entry.is_file() and entry.suffix.lower() in _EXTENSIONS:
                paths.append(entry)
            elif entry.is_dir() and not entry.name.startswith("."):
                paths.extend(
                    sub
                    for sub in entry.iterdir()
                    if sub.is_file() and sub.suffix.lower() in _EXTENSIONS
                )
        paths.sort(key=lambda path: path.relative_to(self.dir).as_posix())
        return [_piece_for(path, self.dir) for path in paths]

    def eligible(self, min_aspect: float = 0.0) -> list[ArtPiece]:
        """The pieces that may rotate: those at least ``min_aspect`` wide.

        A piece whose image cannot be read, or that is narrower than
        ``min_aspect``, is left out. The aspect cache means an unchanged image is
        decoded once rather than on every call.
        """
        result = []
        for piece in self.pieces():
            aspect = self._aspect(piece)
            if aspect is not None and aspect >= min_aspect:
                result.append(piece)
        return result

    def pick(
        self, now: datetime, rotate_minutes: int, min_aspect: float = 0.0
    ) -> ArtPiece | None:
        """The piece to show at ``now``, stable within each rotation slot.

        Walks the slot's shuffled order and returns the first piece that both
        decodes and is at least ``min_aspect`` times wider than tall, so a
        corrupt file or a too-tall piece is skipped rather than blanking the
        slot. Returns ``None`` only when no piece qualifies, which the caller
        shows as the placeholder.
        """
        pieces = self.pieces()
        if not pieces:
            return None
        epoch_minutes = int(now.timestamp()) // 60
        slot = art_slot(now, rotate_minutes)
        day = epoch_minutes // _MINUTES_PER_DAY
        order = list(pieces)
        Random(day).shuffle(order)
        start = slot % len(order)
        for offset in range(len(order)):
            piece = order[(start + offset) % len(order)]
            aspect = self._aspect(piece)
            if aspect is not None and aspect >= min_aspect:
                return piece
        return None

    def _aspect(self, piece: ArtPiece) -> float | None:
        """The piece's width/height, cached, or ``None`` if it cannot be read.

        The cache is keyed by the file's mtime and size, so a replaced image is
        re-measured while an unchanged one is measured once. A file that fails
        to decode returns ``None`` and is not cached, matching the corrupt-file
        skip in ``pick``.
        """
        try:
            stat = piece.path.stat()
        except OSError:
            return None
        cached = self._aspect_cache.get(piece.path)
        if cached is not None and cached[:2] == (stat.st_mtime, stat.st_size):
            return cached[2]
        image = load_piece_image(piece)
        if image is None:
            return None
        width, height = image.size
        if height == 0:
            return None
        aspect = width / height
        self._aspect_cache[piece.path] = (stat.st_mtime, stat.st_size, aspect)
        return aspect


def art_slot(now: datetime, rotate_minutes: int) -> int:
    """The rotation slot ``now`` falls in, for the stateless ``pick`` preview.

    Buckets absolute epoch minutes, so it is independent of any timezone or DST
    shift. The live loop rotates through the stateful ``Rotation`` machine
    instead; ``pick`` and ``art_slot`` serve the ``preview art`` dev command,
    which has no state file.
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


def _piece_for(path: Path, root: Path) -> ArtPiece:
    title, artist, source_url = _caption(path)
    return ArtPiece(
        path=path,
        title=title,
        artist=artist,
        source_url=source_url,
        id=path.relative_to(root).as_posix(),
    )


def _caption(path: Path) -> tuple[str, str | None, str | None]:
    # Lines are positional: title, artist, source URL. Blank lines are kept so
    # an empty artist does not shift the URL up into its place.
    sidecar = path.with_name(path.name + ".txt")
    if sidecar.is_file():
        lines = [line.strip() for line in sidecar.read_text().splitlines()]
        if lines and lines[0]:
            title = lines[0]
            artist = lines[1] if len(lines) > 1 and lines[1] else None
            source_url = _source_url(lines[2]) if len(lines) > 2 else None
            return title, artist, source_url
    return _title_from_filename(path), None, None


def _source_url(value: str) -> str | None:
    """The sidecar's third line as a source URL, kept only when it is https."""
    parsed = urlparse(value)
    if parsed.scheme == "https" and parsed.netloc:
        return value
    return None


def _title_from_filename(path: Path) -> str:
    return path.stem.replace("_", " ").replace("-", " ")
