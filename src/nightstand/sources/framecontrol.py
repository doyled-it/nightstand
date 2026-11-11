"""The hub's frame-control file.

The hub's ``snaphouse-artadmin`` service publishes ``frame-control.json`` in its
public web root, served same-origin by nginx. It holds the frame settings (which
kinds of audio take the frame, the quiet-hours window, and the morning-weather
window) and the current pin (hold the art, the weather, or now playing for a
while). The frame reads it each cycle, exactly as it reads ``art-control.json``:
the settings override the local config and the pin overrides the normal schedule.

``fetch_control`` GETs the file and reduces it to a ``FrameControl``. A missing,
unreachable, or malformed file (no response, bad JSON, or no usable ``settings``)
returns ``None`` so the caller falls back to its local config and no pin, exactly
as a missing now-playing file means nothing playing. Every field is validated,
and a garbage value falls back to its default rather than discarding the whole
control. The shape is pinned in the hub's ``docs/cross-repo-features.md`` (the
"Frame control file" contract).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

import httpx

logger = logging.getLogger(__name__)

_TIMEOUT = 5.0

#: The three audio kinds the settings may enable for the frame. Anything else in
#: the list is ignored, so a spoofed or buggy file cannot name an unknown kind.
VALID_KINDS = ("music", "podcast", "audiobook")
#: The default when the hub names no kinds (a missing or garbage value). An
#: explicit empty list is honoured instead: the owner chose that nothing takes
#: the frame.
DEFAULT_KINDS = ("music",)

#: What a pin may hold on the frame.
PIN_TARGETS = ("art", "weather", "now_playing")


@dataclass(frozen=True)
class MorningWeather:
    """The morning-weather window, and whether it is enabled at all."""

    enabled: bool
    weekday: tuple[str, str]
    weekend: tuple[str, str]


@dataclass(frozen=True)
class FrameSettings:
    """The frame settings from the hub's control file.

    ``now_playing_kinds`` is the set of audio categories that may take the frame.
    ``quiet_hours`` is ``(start, end)`` or ``None`` when the hub sets none, in
    which case the frame keeps its local config. ``morning_weather`` is the
    morning window and its on/off flag, or ``None`` to keep the local config.
    """

    now_playing_kinds: tuple[str, ...]
    quiet_hours: tuple[str, str] | None
    morning_weather: MorningWeather | None


@dataclass(frozen=True)
class Pin:
    """The current pin: hold one screen on the frame, ignoring the schedule.

    ``what`` is ``"art"``, ``"weather"``, or ``"now_playing"``. ``piece`` is the
    pinned art id for an art pin (the piece showing when it was set), else
    ``None``. ``until`` is when the pin expires, or ``None`` to hold until
    unpinned. ``seq`` increases on every pin or unpin, so the frame can tell a
    change and redraw once. ``set_at`` is informational.
    """

    what: str
    piece: str | None
    until: datetime | None
    seq: int
    set_at: datetime | None


@dataclass(frozen=True)
class FrameControl:
    """The frame settings and the current pin from the hub's control file."""

    settings: FrameSettings
    pin: Pin | None


def fetch_control(client: httpx.Client, host: str) -> FrameControl | None:
    """Read ``http://{host}/frame-control.json`` into a ``FrameControl``, or ``None``.

    ``host`` is the hub's HTTP authority (host, or host:port off the default port
    80). Returns ``None`` for anything the frame cannot use, which the caller
    treats as "no hub control, use the local config and no pin".
    """
    doc = _get(client, host)
    if not isinstance(doc, dict):
        return None
    raw_settings = doc.get("settings")
    if not isinstance(raw_settings, dict):
        return None
    return FrameControl(
        settings=_settings(raw_settings), pin=_pin(doc.get("pin"))
    )


def _settings(raw: dict) -> FrameSettings:
    return FrameSettings(
        now_playing_kinds=_kinds(raw.get("now_playing_kinds")),
        quiet_hours=_window(raw.get("quiet_hours")),
        morning_weather=_morning(raw.get("morning_weather")),
    )


def _kinds(value: object) -> tuple[str, ...]:
    """The enabled kinds: valid entries of a list, else the default.

    An explicit (possibly empty) list is honoured after filtering to the three
    valid kinds; a missing or non-list value falls back to the default. A list
    naming only unknown kinds filters to empty by design, silencing the
    now-playing screen, since an explicit list is taken at its word; only a
    non-list value defaults to music.
    """
    if not isinstance(value, list):
        return DEFAULT_KINDS
    return tuple(k for k in value if k in VALID_KINDS)


def _window(value: object) -> tuple[str, str] | None:
    """A ``(start, end)`` pair of ``HH:MM`` strings, or ``None`` when unusable."""
    if not isinstance(value, dict):
        return None
    start = value.get("start")
    end = value.get("end")
    if _valid_hhmm(start) and _valid_hhmm(end):
        return (start, end)
    return None


def _morning(value: object) -> MorningWeather | None:
    """The morning-weather window, or ``None`` when any part is unusable."""
    if not isinstance(value, dict):
        return None
    enabled = value.get("enabled")
    if not isinstance(enabled, bool):
        return None
    weekday = _pair(value.get("weekday"))
    weekend = _pair(value.get("weekend"))
    if weekday is None or weekend is None:
        return None
    return MorningWeather(enabled=enabled, weekday=weekday, weekend=weekend)


def _pair(value: object) -> tuple[str, str] | None:
    """A two-element list of ``HH:MM`` strings as a tuple, or ``None``."""
    if not isinstance(value, list) or len(value) != 2:
        return None
    start, end = value
    if _valid_hhmm(start) and _valid_hhmm(end):
        return (start, end)
    return None


def _pin(value: object) -> Pin | None:
    """The pin, or ``None`` when absent or unusable.

    An unknown target or a missing/non-integer ``seq`` drops the pin entirely; an
    unparseable ``until`` degrades to "forever" rather than dropping the pin.
    """
    if not isinstance(value, dict):
        return None
    what = value.get("what")
    if what not in PIN_TARGETS:
        return None
    raw_seq = value.get("seq")
    if not isinstance(raw_seq, int) or isinstance(raw_seq, bool):
        return None
    piece = value.get("piece")
    return Pin(
        what=what,
        piece=piece if isinstance(piece, str) else None,
        until=_time(value.get("until")),
        seq=raw_seq,
        set_at=_time(value.get("set_at")),
    )


def _valid_hhmm(value: object) -> bool:
    """Whether ``value`` is a ``HH:MM`` 24-hour clock string."""
    if not isinstance(value, str):
        return False
    parts = value.split(":")
    if len(parts) != 2:
        return False
    hours, minutes = parts
    if not (hours.isdigit() and minutes.isdigit()):
        return False
    return 0 <= int(hours) <= 23 and 0 <= int(minutes) <= 59


def _time(value: object) -> datetime | None:
    """An aware ``datetime`` from an ISO-8601 string, or ``None`` when unusable.

    A naive stamp (one with no UTC offset) is rejected: the contract requires an
    offset, and comparing a naive ``until`` with the aware clock would raise and
    stop the loop, so it is treated as no timestamp rather than reaching
    ``active_pin``.
    """
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def _get(client: httpx.Client, host: str) -> object | None:
    """The parsed control document, or ``None`` when it cannot be read."""
    try:
        response = client.get(f"http://{host}/frame-control.json", timeout=_TIMEOUT)
    except httpx.HTTPError as error:
        logger.debug("frame control fetch failed: %s", error)
        return None
    if response.status_code != 200:
        logger.debug("frame control returned %s", response.status_code)
        return None
    try:
        return response.json()
    except ValueError as error:
        logger.debug("frame control was not JSON: %s", error)
        return None
