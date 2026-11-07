"""The published art-history file.

Each time the app pushes the art screen, it records the piece here: an atomic
rewrite of ``art-history.json`` in the nginx-served public directory, newest
first and capped at the most recent fifty. snaphouse's nginx on the nightstand
host serves it at ``/nightstand/art-history.json`` so the hub can show what the
frame has been displaying.

The shape is pinned in the cross-repo contract:
``{"current": Piece | null, "history": [Piece, ...]}`` with ``history`` newest
first, ``current`` the same object as ``history[0]``, and each ``Piece``
``{"file", "title", "artist", "source_url", "shown_at"}``. A piece that is
already current is not recorded again, so a redraw of the same piece (a health
marker flip, say) in the same slot does not pile up duplicates. The file is
loaded at startup so history survives restarts, and a missing or corrupt file
is tolerated by starting fresh with one warning.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path

from nightstand.models import ArtPiece

logger = logging.getLogger(__name__)

MAX_ENTRIES = 50
_FILE_MODE = 0o644


class ArtHistory:
    """The art-history file: append a shown piece, cap, and rewrite atomically."""

    def __init__(self, path: Path, *, max_entries: int = MAX_ENTRIES) -> None:
        self.path = Path(path)
        self._max = max_entries
        self._entries: list[dict] = self._load()

    def record(self, piece: ArtPiece, shown_at: datetime) -> bool:
        """Record ``piece`` as shown at ``shown_at``; return whether it was added.

        Returns ``False`` without writing when ``piece`` is already the current
        entry, so repeated pushes of the same piece do not add duplicates.
        """
        entry = {
            "file": piece.path.name,
            "title": piece.title,
            "artist": piece.artist,
            "source_url": piece.source_url,
            "shown_at": shown_at.isoformat(),
        }
        if self._entries and self._entries[0].get("file") == entry["file"]:
            return False
        self._entries.insert(0, entry)
        del self._entries[self._max :]
        self._write()
        return True

    def _load(self) -> list[dict]:
        if not self.path.is_file():
            return []
        try:
            data = json.loads(self.path.read_text())
            history = data["history"]
            if not isinstance(history, list):
                raise ValueError("history is not a list")
            entries = [entry for entry in history if isinstance(entry, dict)]
            return entries[: self._max]
        except (OSError, ValueError, KeyError) as error:
            logger.warning("art history unreadable, starting fresh: %s", error)
            return []

    def _write(self) -> None:
        payload = {
            "current": self._entries[0] if self._entries else None,
            "history": self._entries,
        }
        text = json.dumps(payload, indent=2) + "\n"
        directory = self.path.parent
        directory.mkdir(parents=True, exist_ok=True)
        # Write a uniquely named temp file in the same directory, then rename it
        # into place so a reader never sees a half-written file.
        fd, tmp = tempfile.mkstemp(
            dir=directory, prefix=".art-history-", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
            os.chmod(tmp, _FILE_MODE)
            os.replace(tmp, self.path)
        except OSError:
            _unlink_quietly(tmp)
            raise


def _unlink_quietly(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass
