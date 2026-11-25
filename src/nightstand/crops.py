"""Crop boxes the hub publishes for each art piece, and what the frame drew.

The hub's ``art-index.json`` carries, per piece, ``crop`` (the owner's saved box)
and ``auto_crop`` (the hub's automatic box), each ``{"x", "y", "w", "h"}`` as
fractions of the UPRIGHT picture, or ``null``. ``artsync`` keeps them in a small
JSON map in the state dir (``CropMap``); the art library attaches them to each
piece; the art screen cuts the box. ``save_drawn`` and ``load_drawn`` record the
box the frame last drew, so a change to the shown piece's box is noticed.

Everything here is soft: a missing, malformed or out-of-range box is ignored and
never raises, so the frame falls back to ``detail_crop``.
"""

from __future__ import annotations

import json
import logging
import math
import os
import tempfile
from pathlib import Path

from nightstand.models import Box

logger = logging.getLogger(__name__)

#: File names in the state dir: the synced boxes, and the box last drawn.
CROPS_FILE = "art-crops.json"
DRAWN_FILE = "art-drawn.json"
_FILE_MODE = 0o644
_KEYS = ("x", "y", "w", "h")
# Slack for float noise at the picture's edge (values are stored to six places).
_EPS = 1e-6

Entry = tuple[Box | None, Box | None]


def parse_box(value: object) -> Box | None:
    """A ``{"x","y","w","h"}`` dict as an ``(x, y, w, h)`` tuple, or ``None``.

    ``None`` for a wrong type, a bool or string value, NaN or infinity, a zero or
    negative size, a negative origin, or a box that leaves the picture.
    """
    if not isinstance(value, dict) or set(value) != set(_KEYS):
        return None
    numbers = []
    for key in _KEYS:
        number = value[key]
        if isinstance(number, bool) or not isinstance(number, (int, float)):
            return None
        number = float(number)
        if not math.isfinite(number):
            return None
        numbers.append(number)
    x, y, w, h = numbers
    if x < 0 or y < 0 or w <= 0 or h <= 0:
        return None
    if x + w > 1 + _EPS or y + h > 1 + _EPS:
        return None
    return (x, y, w, h)


def as_dict(box: Box | dict | None) -> dict | None:
    if box is None:
        return None
    if isinstance(box, dict):
        return box
    if not isinstance(box, (tuple, list)):
        return None
    return dict(zip(_KEYS, box, strict=False))


class CropMap:
    """The synced boxes, keyed by piece id: ``{id: (crop, auto_crop)}``."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._cache: tuple[tuple[int, int], dict[str, Entry]] | None = None

    def load(self) -> dict[str, Entry]:
        """Every piece's boxes; an unreadable file or box is ignored."""
        try:
            stat = self._path.stat()
        except OSError:
            self._cache = None
            return {}
        signature = (stat.st_mtime_ns, stat.st_size)
        if self._cache is not None and self._cache[0] == signature:
            return self._cache[1]
        entries = self._read()
        self._cache = (signature, entries)
        return entries

    def _read(self) -> dict[str, Entry]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        pieces = data.get("pieces") if isinstance(data, dict) else None
        if not isinstance(pieces, dict):
            return {}
        entries: dict[str, Entry] = {}
        for pid, item in pieces.items():
            if not isinstance(pid, str) or not isinstance(item, dict):
                continue
            entries[pid] = (
                parse_box(item.get("crop")),
                parse_box(item.get("auto_crop")),
            )
        return entries

    def save(
        self, entries: dict[str, tuple[Box | dict | None, Box | dict | None]]
    ) -> None:
        """Write the map atomically. Raises ``OSError`` when it cannot."""
        pieces = {
            pid: {"crop": as_dict(crop), "auto_crop": as_dict(auto)}
            for pid, (crop, auto) in sorted(entries.items())
        }
        _write_json(self._path, {"pieces": pieces})
        self._cache = None


def save_drawn(path: Path, piece_id: str, box: Box | None) -> None:
    """Record the piece and effective box the frame just drew. Raises ``OSError``."""
    _write_json(Path(path), {"id": piece_id, "box": as_dict(box)})


def load_drawn(path: Path) -> tuple[str, Box | None] | None:
    """The piece and box last drawn, or ``None`` when unknown or unreadable."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("id"), str):
        return None
    return data["id"], parse_box(data.get("box"))


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".crops-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, indent=2) + "\n")
        os.chmod(tmp, _FILE_MODE)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
