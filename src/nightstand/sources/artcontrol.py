"""The hub's rotation control file.

The hub's ``snaphouse-artadmin`` service publishes ``art-control.json`` in its
public web root, served same-origin by nginx. It holds the rotation settings
(how often to rotate, which folders rotate) and the last command the room-picker
page issued (next, previous, or show a piece). The frame reads it each cycle:
the settings override the local config and the command drives the rotation state
machine.

``fetch_control`` GETs the file and reduces it to an ``ArtControl``. A missing,
unreachable, or malformed file (no response, bad JSON, or no usable ``settings``)
returns ``None`` so the caller falls back to its local config, exactly as a
missing now-playing file means nothing playing. The shape is pinned in the hub's
``docs/cross-repo-features.md`` (the "Art control file" contract).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

_TIMEOUT = 5.0

#: The rotation intervals the page offers, in minutes. A hub value outside this
#: set is ignored and the default is used, so a spoofed or buggy control file
#: cannot drive the panel to an unsupported cadence.
ALLOWED_INTERVALS = frozenset({15, 30, 60, 120, 180, 360, 720, 1440})
DEFAULT_INTERVAL = 60

_ACTIONS = ("next", "previous", "show")


@dataclass(frozen=True)
class ArtControl:
    """The rotation settings and last command from the hub's control file.

    ``rotate_minutes``, ``folders_enabled``, and ``min_aspect`` are the settings;
    ``seq``, ``action``, and ``piece`` are the last command, all ``None`` when the
    hub has issued no command yet. ``seq`` only increases, so a frame acts on a
    command whose ``seq`` is greater than the last one it applied. ``min_aspect``
    is the rotation cutoff (the one source of truth for every frame) and is
    ``None`` when the hub does not set one, so the frame keeps its local config.
    """

    rotate_minutes: int
    folders_enabled: tuple[str, ...]
    seq: int | None
    action: str | None
    piece: str | None
    min_aspect: float | None = None


def fetch_control(client: httpx.Client, host: str) -> ArtControl | None:
    """Read ``http://{host}/art-control.json`` into an ``ArtControl``, or ``None``.

    ``host`` is the hub's HTTP authority (host, or host:port off the default port
    80). Returns ``None`` for anything the frame cannot use, which the caller
    treats as "no hub control, use the local config".
    """
    doc = _get(client, host)
    if not isinstance(doc, dict):
        return None
    settings = doc.get("settings")
    if not isinstance(settings, dict):
        return None

    rotate = settings.get("rotate_minutes")
    rotate_minutes = rotate if rotate in ALLOWED_INTERVALS else DEFAULT_INTERVAL

    folders = settings.get("folders_enabled")
    folders_enabled = (
        tuple(f for f in folders if isinstance(f, str))
        if isinstance(folders, list)
        else ()
    )

    raw_aspect = settings.get("min_aspect")
    min_aspect = raw_aspect if _is_aspect(raw_aspect) else None

    seq, action, piece = _command(doc.get("command"))
    return ArtControl(
        rotate_minutes=rotate_minutes,
        folders_enabled=folders_enabled,
        seq=seq,
        action=action,
        piece=piece,
        min_aspect=min_aspect,
    )


def _command(command: object) -> tuple[int | None, str | None, str | None]:
    """The command's ``(seq, action, piece)``, each ``None`` when absent or bad."""
    if not isinstance(command, dict):
        return None, None, None
    raw_seq = command.get("seq")
    # bool is an int subclass but never a sequence number.
    seq = raw_seq if _is_seq(raw_seq) else None
    action = command.get("action")
    if action not in _ACTIONS:
        action = None
    raw_piece = command.get("piece")
    piece = raw_piece if isinstance(raw_piece, str) else None
    return seq, action, piece


def _is_seq(value: object) -> bool:
    """Whether ``value`` is a usable command sequence number (an int, not a bool)."""
    return isinstance(value, int) and not isinstance(value, bool)


def _is_aspect(value: object) -> bool:
    """Whether ``value`` is a usable aspect cutoff: a positive number, not a bool."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


def _get(client: httpx.Client, host: str) -> object | None:
    """The parsed control document, or ``None`` when it cannot be read."""
    try:
        response = client.get(f"http://{host}/art-control.json", timeout=_TIMEOUT)
    except httpx.HTTPError as error:
        logger.debug("art control fetch failed: %s", error)
        return None
    if response.status_code != 200:
        logger.debug("art control returned %s", response.status_code)
        return None
    try:
        return response.json()
    except ValueError as error:
        logger.debug("art control was not JSON: %s", error)
        return None
