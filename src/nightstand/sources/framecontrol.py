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
import math
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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

#: A frame whose quiet hours are explicitly off (``frames[<id>].quiet_hours`` is
#: ``{"enabled": false}``) is encoded as a zero-length window: ``start == end``,
#: which ``rules.in_quiet_hours`` reads as "never quiet". This keeps the control
#: layer producing a plain ``(start, end)`` that ``effective_config`` overlays
#: with no special case, distinct from ``None`` (no override, keep the shared or
#: local config).
QUIET_HOURS_OFF = ("00:00", "00:00")

#: The single frame in the pre-multi-frame contract. A document with no ``frames``
#: map is the old shape, whose top-level ``pin`` is this frame's only when it is
#: the nightstand (Review Focus 3); another frame reading an old document gets no
#: pin.
LEGACY_FRAME_ID = "nightstand"


@dataclass(frozen=True)
class MorningWeather:
    """The morning-weather window, and whether it is enabled at all."""

    enabled: bool
    weekday: tuple[str, str]
    weekend: tuple[str, str]


@dataclass(frozen=True)
class WeatherLocation:
    """A weather city set from the hub, replacing the local ``[location]``.

    The hub geocodes a typed city into these fields and publishes them so every
    frame shows the same place. ``latitude`` and ``longitude`` are city-level
    (rounded to two decimals by the hub) and ``timezone`` is an IANA name;
    ``name`` is the display label and is informational. ``None`` for the whole
    value (a missing, null, or invalid block) means the frame keeps its local
    ``[location]``.
    """

    name: str | None
    latitude: float
    longitude: float
    timezone: str


@dataclass(frozen=True)
class FrameSettings:
    """The frame settings from the hub's control file.

    ``now_playing_kinds`` is the set of audio categories that may take the frame.
    ``quiet_hours`` is ``(start, end)`` or ``None`` when the hub sets none, in
    which case the frame keeps its local config. ``morning_weather`` is the
    morning window and its on/off flag, or ``None`` to keep the local config.
    ``weather_location`` is the hub's weather city, or ``None`` to keep the local
    ``[location]``.
    """

    now_playing_kinds: tuple[str, ...]
    quiet_hours: tuple[str, str] | None
    morning_weather: MorningWeather | None
    weather_location: WeatherLocation | None = None


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


def fetch_control(
    client: httpx.Client, host: str, frame_id: str
) -> FrameControl | None:
    """Read ``http://{host}/frame-control.json`` into a ``FrameControl``, or ``None``.

    ``host`` is the hub's HTTP authority (host, or host:port off the default port
    80). ``frame_id`` is this frame's inventory id, which selects its slice of the
    ``frames`` map: its own quiet-hours override and pin. The shared ``settings``
    apply to every frame. A document with no ``frames`` map is the old
    single-frame shape, whose top-level ``pin`` is the nightstand's (Review Focus
    3). Returns ``None`` for anything the frame cannot use, which the caller
    treats as "no hub control, use the local config and no pin".
    """
    doc = _get(client, host)
    if not isinstance(doc, dict):
        return None
    raw_settings = doc.get("settings")
    if not isinstance(raw_settings, dict):
        return None
    entry = _frame_entry(doc.get("frames"), frame_id)
    shared_quiet = _window(raw_settings.get("quiet_hours"))
    quiet_hours = _frame_quiet_hours(entry, shared_quiet)
    settings = _settings(raw_settings, quiet_hours)
    return FrameControl(settings=settings, pin=_frame_pin(doc, entry, frame_id))


def _settings(raw: dict, quiet_hours: tuple[str, str] | None) -> FrameSettings:
    return FrameSettings(
        now_playing_kinds=_kinds(raw.get("now_playing_kinds")),
        quiet_hours=quiet_hours,
        morning_weather=_morning(raw.get("morning_weather")),
        weather_location=_weather_location(raw.get("weather_location")),
    )


def _frame_entry(frames: object, frame_id: str) -> dict | None:
    """This frame's entry in the ``frames`` map, or ``None``.

    ``None`` when there is no ``frames`` map (the old single-frame shape), when
    this frame is not listed in it (the hub does not know the frame yet), or when
    the entry is not an object.
    """
    if not isinstance(frames, dict):
        return None
    entry = frames.get(frame_id)
    return entry if isinstance(entry, dict) else None


def _frame_quiet_hours(
    entry: dict | None, shared: tuple[str, str] | None
) -> tuple[str, str] | None:
    """This frame's effective quiet hours.

    ``entry`` is this frame's control entry, or ``None`` for the old shape or a
    frame the hub does not list. Its ``quiet_hours`` override may be absent or
    ``null`` (fall back to the shared ``settings.quiet_hours``), ``{"enabled":
    false}`` (off, so the frame never sleeps), or a window ``{"start", "end"}``
    (an override). Any other, malformed value falls back to the shared value, so
    a garbage override never silently disables quiet hours.
    """
    if entry is None or "quiet_hours" not in entry:
        return shared
    raw = entry.get("quiet_hours")
    if isinstance(raw, dict):
        if raw.get("enabled") is False:
            return QUIET_HOURS_OFF
        window = _window(raw)
        if window is not None:
            return window
    return shared


def _frame_pin(doc: dict, entry: dict | None, frame_id: str) -> Pin | None:
    """This frame's pin.

    New shape: the pin in this frame's ``frames`` entry (no entry means no pin).
    Old shape (no ``frames`` map): the top-level ``pin`` is the nightstand's, so
    the nightstand keeps working when the hub updates last, and another frame
    reading an old document gets no pin (Review Focus 3).
    """
    if isinstance(doc.get("frames"), dict):
        return _pin(entry.get("pin")) if entry is not None else None
    if frame_id == LEGACY_FRAME_ID:
        return _pin(doc.get("pin"))
    return None


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


def _weather_location(value: object) -> WeatherLocation | None:
    """The hub's weather city, or ``None`` when absent or unusable.

    ``None``, a missing key, or a non-object means "no hub location, keep the
    local ``[location]``". A present block must carry a latitude in -90..90, a
    longitude in -180..180, and a valid IANA ``timezone``; any part out of range
    or malformed drops the whole location rather than fetching a bad forecast.
    """
    if not isinstance(value, dict):
        return None
    latitude = _coord(value.get("latitude"), 90.0)
    longitude = _coord(value.get("longitude"), 180.0)
    if latitude is None or longitude is None:
        return None
    timezone = value.get("timezone")
    if not _valid_zone(timezone):
        return None
    name = value.get("name")
    return WeatherLocation(
        name=name if isinstance(name, str) else None,
        latitude=latitude,
        longitude=longitude,
        timezone=timezone,
    )


def _coord(value: object, limit: float) -> float | None:
    """``value`` as a finite float within ``+/- limit``, or ``None``.

    Excludes ``bool`` (a subclass of ``int``), rejects NaN and infinities, and
    treats an out-of-float-range integer (``10**400``, say) as unusable rather
    than letting the ``OverflowError`` escape and drop the whole control document.
    """
    if not _is_number(value):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    if -limit <= number <= limit:
        return number
    return None


def _is_number(value: object) -> bool:
    """A real int or float, excluding ``bool`` (a subclass of ``int``)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _valid_zone(value: object) -> bool:
    """Whether ``value`` names a timezone ``zoneinfo`` can load."""
    if not isinstance(value, str) or not value:
        return False
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


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
