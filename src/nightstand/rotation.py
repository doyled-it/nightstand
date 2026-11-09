"""The art rotation state machine.

The frame follows the hub's library and settings, but it keeps its own place in
the rotation here: the piece on the panel now, when it was last changed, the
last command it applied, and a short back stack of pieces it showed so "previous"
can walk back through them. The state lives in a small JSON file in the frame's
state directory so a restart resumes the current piece rather than jumping.

``current`` returns the piece to show, advancing when the interval has passed
since the last change (a rotation step, next, previous, or show), not on fixed
clock slots, so pressing a button does not get overridden a minute later.
``apply`` runs a command (next, previous, show) and returns whether it changed
the current piece, which the loop uses to force an immediate redraw. The order is
the eligible pieces shuffled once per day, as the collection rotated before.

Rules pinned by the design and Review Focus 4 and 5:

- The first command a process sees at startup only records its ``seq``; it is not
  replayed, so a frame that boots to an old command resumes its piece.
- A command's ``seq`` only increases; one already applied is ignored.
- A show of an unknown or ineligible piece is ignored.
- No eligible pieces gives ``None`` (the loop shows the placeholder).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from random import Random

from nightstand.models import ArtPiece
from nightstand.sources.artcontrol import ArtControl

logger = logging.getLogger(__name__)

_MINUTES_PER_DAY = 60 * 24
_BACK_STACK_MAX = 20
_FILE_MODE = 0o644


class Rotation:
    """The frame's place in the rotation, persisted across restarts."""

    def __init__(
        self, state_path: Path, clock: Callable[[], datetime]
    ) -> None:
        self._path = Path(state_path)
        self._clock = clock
        self._current_id: str | None = None
        self._shown_at: datetime | None = None
        self._last_seq: int | None = None
        self._back: list[str] = []
        # The startup grace: the first ``apply`` of this process adopts the
        # command's seq as a baseline without replaying it (Review Focus 4).
        self._first_apply = True
        self._load()

    # -- reading the current piece ------------------------------------------- #

    def current(
        self,
        eligible: list[ArtPiece],
        now: datetime | None = None,
        interval_minutes: int = 60,
    ) -> ArtPiece | None:
        """The piece to show at ``now``, advancing once the interval has passed.

        Returns ``None`` when nothing is eligible. Otherwise it keeps the current
        piece while it is still eligible and the interval has not elapsed since it
        was shown, and steps to the next piece in the day's shuffled order
        otherwise.
        """
        now = now if now is not None else self._clock()
        ids = {piece.id: piece for piece in eligible}
        if not ids:
            return None
        if (
            self._current_id in ids
            and self._shown_at is not None
            and now - self._shown_at < timedelta(minutes=interval_minutes)
        ):
            return ids[self._current_id]
        order = self._shuffled(eligible, now)
        nxt = self._next_in(order)
        self._move_to(nxt, now, push_current=True, eligible_ids=ids)
        return ids[nxt]

    # -- applying a command -------------------------------------------------- #

    def apply(
        self,
        command: ArtControl,
        eligible: list[ArtPiece],
        now: datetime | None = None,
    ) -> bool:
        """Run ``command``; return ``True`` only if it changed the current piece.

        The first call of the process consumes the startup grace: an existing
        command is adopted as the baseline without acting. Thereafter a command
        whose ``seq`` is greater than the last applied one runs.
        """
        now = now if now is not None else self._clock()
        first = self._first_apply
        self._first_apply = False

        if command.seq is None:
            return False
        if self._last_seq is not None and command.seq <= self._last_seq:
            return False
        if first:
            self._last_seq = command.seq
            self._persist()
            return False

        changed = self._execute(command, eligible, now)
        self._last_seq = command.seq
        self._persist()
        return changed

    def _execute(
        self, command: ArtControl, eligible: list[ArtPiece], now: datetime
    ) -> bool:
        ids = {piece.id: piece for piece in eligible}
        if not ids:
            return False
        if command.action == "next":
            order = self._shuffled(eligible, now)
            return self._move_to(
                self._next_in(order), now, push_current=True, eligible_ids=ids
            )
        if command.action == "previous":
            return self._go_back(now, ids)
        if command.action == "show":
            if command.piece not in ids:
                return False
            return self._move_to(
                command.piece, now, push_current=True, eligible_ids=ids
            )
        return False

    # -- moving between pieces ----------------------------------------------- #

    def _move_to(
        self,
        new_id: str,
        now: datetime,
        *,
        push_current: bool,
        eligible_ids: dict[str, ArtPiece],
    ) -> bool:
        """Make ``new_id`` the current piece; return whether it actually changed.

        The piece being left is pushed onto the back stack when it is still a real
        eligible piece, so "previous" walks back through what was shown. Re-showing
        the same piece (a single-piece library, say) refreshes the clock without a
        change.
        """
        changed = new_id != self._current_id
        if (
            changed
            and push_current
            and self._current_id is not None
            and self._current_id in eligible_ids
        ):
            self._push_back(self._current_id)
        self._current_id = new_id
        self._shown_at = now
        self._persist()
        return changed

    def _go_back(self, now: datetime, eligible_ids: dict[str, ArtPiece]) -> bool:
        """Pop the back stack to a still-eligible earlier piece, if any."""
        changed = False
        while self._back:
            candidate = self._back.pop()
            if candidate in eligible_ids and candidate != self._current_id:
                self._current_id = candidate
                self._shown_at = now
                changed = True
                break
        self._persist()
        return changed

    def _push_back(self, piece_id: str) -> None:
        self._back.append(piece_id)
        del self._back[:-_BACK_STACK_MAX]

    # -- the daily shuffle --------------------------------------------------- #

    def _shuffled(self, eligible: list[ArtPiece], now: datetime) -> list[str]:
        order = sorted(piece.id for piece in eligible)
        day = int(now.timestamp()) // 60 // _MINUTES_PER_DAY
        Random(day).shuffle(order)
        return order

    def _next_in(self, order: list[str]) -> str:
        if self._current_id in order:
            index = order.index(self._current_id)
            return order[(index + 1) % len(order)]
        return order[0]

    # -- persistence --------------------------------------------------------- #

    def _load(self) -> None:
        if not self._path.is_file():
            return
        try:
            data = json.loads(self._path.read_text())
        except (OSError, ValueError) as error:
            logger.warning("rotation state unreadable, starting fresh: %s", error)
            return
        if not isinstance(data, dict):
            return
        current = data.get("current_id")
        self._current_id = current if isinstance(current, str) else None
        self._shown_at = _parse_time(data.get("shown_at"))
        seq = data.get("last_seq")
        if isinstance(seq, int) and not isinstance(seq, bool):
            self._last_seq = seq
        back = data.get("back")
        if isinstance(back, list):
            kept = [item for item in back if isinstance(item, str)]
            self._back = kept[-_BACK_STACK_MAX:]

    def _persist(self) -> None:
        payload = {
            "current_id": self._current_id,
            "shown_at": self._shown_at.isoformat() if self._shown_at else None,
            "last_seq": self._last_seq,
            "back": self._back,
        }
        text = json.dumps(payload, indent=2) + "\n"
        directory = self._path.parent
        try:
            directory.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(
                dir=directory, prefix=".rotation-", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(text)
                os.chmod(tmp, _FILE_MODE)
                os.replace(tmp, self._path)
            except OSError:
                _unlink_quietly(tmp)
                raise
        except OSError as error:
            # Persisting is best effort: a read-only state dir must never stop
            # the display loop. The in-memory state still drives this process.
            logger.warning("rotation state write failed: %s", error)


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _unlink_quietly(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass
