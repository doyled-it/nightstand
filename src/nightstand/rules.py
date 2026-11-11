"""The scheduling rules: what to show now, and whether to redraw.

This module is pure. It reads no clock and does no I/O: every function takes an
aware ``now`` and a ``Config``, so the same inputs always give the same answer
and the edges are easy to test with a fixed clock.

``decide`` walks ``cfg.schedule.priority`` and returns the first screen whose
condition holds, falling through to the placeholder. ``should_push`` then gates
the actual redraw: it suppresses a repeat of what is already on screen, and it
keeps the panel still during quiet hours for every screen except the game, which
is allowed to keep updating under its own redraw key.

Quiet hours and the morning window are evaluated in local wall-clock time:
``now`` converted to ``cfg.location.timezone`` when set, otherwise the system
local zone. Converting through the zone (not a fixed offset) keeps the windows
anchored to the clock on the wall across daylight-saving transitions.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from nightstand.config import Config
from nightstand.models import ArtPiece, NowPlaying
from nightstand.sources.framecontrol import FrameControl, FrameSettings, Pin
from nightstand.sources.mlb import GameState, game_redraw_key
from nightstand.sources.weather import Forecast

# Genre words that name a category. The genre decides over the kind: an
# audiobook genre on a short music track is an audiobook, and a podcast genre is
# a podcast. A long-form track with no telling genre is a podcast (libro.fm and
# some apps send no genre), and everything else is music.
_AUDIOBOOK_WORDS = ("audiobook", "audio book", "spoken")
_PODCAST_WORDS = ("podcast",)

Screen = Literal["art", "now_playing", "game", "weather", "split", "placeholder"]


@dataclass(frozen=True)
class Context:
    """Everything the current sources produced, each ``None`` when unavailable."""

    now_playing: NowPlaying | None = None
    game: GameState | None = None
    forecast: Forecast | None = None
    art: ArtPiece | None = None


@dataclass(frozen=True)
class Decision:
    """The chosen screen and the key its redraw depends on.

    ``forced`` marks a redraw an art command (next, previous, show) asked for:
    it bypasses the quiet-hours hold, since someone pressed the button and is
    awake. Scheduled rotation leaves it ``False`` and keeps the quiet rule.
    """

    screen: Screen
    redraw_key: tuple
    forced: bool = False


def in_quiet_hours(now: datetime, cfg: Config) -> bool:
    """Whether local ``now`` falls in the quiet-hours window.

    The window is half-open ``[quiet_start, quiet_end)`` and may wrap midnight
    (the default 22:00 to 06:00 does).
    """
    current = _local_minutes(now, cfg)
    start = _minutes(cfg.schedule.quiet_start)
    end = _minutes(cfg.schedule.quiet_end)
    if start <= end:
        return start <= current < end
    return current >= start or current < end


def in_morning_window(now: datetime, cfg: Config) -> bool:
    """Whether local ``now`` falls in the morning window for that day.

    Weekdays (Monday to Friday by ``now.weekday()``) use
    ``schedule.morning_weekday``, Saturday and Sunday use
    ``schedule.morning_weekend``. The window is half-open ``[start, end)``.
    """
    local = _local(now, cfg)
    if local.weekday() < 5:
        start_str, end_str = cfg.schedule.morning_weekday
    else:
        start_str, end_str = cfg.schedule.morning_weekend
    current = local.hour * 60 + local.minute
    return _minutes(start_str) <= current < _minutes(end_str)


def category(np: NowPlaying) -> str:
    """The now-playing category: ``"music"``, ``"podcast"``, or ``"audiobook"``.

    The genre decides when it names a category: an audiobook or spoken-word
    genre is an audiobook, a podcast genre is a podcast. Otherwise a long-form
    track with no telling genre is a podcast (libro.fm and some apps send no
    genre), and everything else is music.
    """
    text = " ".join(g.lower() for g in np.genre)
    if any(word in text for word in _AUDIOBOOK_WORDS):
        return "audiobook"
    if any(word in text for word in _PODCAST_WORDS):
        return "podcast"
    if np.kind == "long_form":
        return "podcast"
    return "music"


def now_playing_for_frame(
    np: NowPlaying | None, kinds: tuple[str, ...], pin: Pin | None
) -> NowPlaying | None:
    """The track that may take the frame, or ``None`` when it may not.

    A now-playing pin shows any kind. Otherwise the track takes the frame only
    when its category is one of the enabled ``kinds``; a track of a disabled kind
    is dropped, so the decision behaves as if nothing were playing.
    """
    if np is None:
        return None
    if pin is not None and pin.what == "now_playing":
        return np
    return np if category(np) in kinds else None


def active_pin(control: FrameControl | None, now: datetime) -> Pin | None:
    """The pin in force at ``now``, or ``None`` when there is none or it expired.

    A pin with no ``until`` holds until unpinned; a pin whose ``until`` has
    passed is expired and returns to the normal schedule.
    """
    if control is None or control.pin is None:
        return None
    pin = control.pin
    if pin.until is not None and now >= pin.until:
        return None
    return pin


def effective_config(cfg: Config, settings: FrameSettings | None) -> Config:
    """``cfg`` with the hub's schedule and weather-location settings overlaid.

    When the hub sets quiet hours or a morning window, they replace the local
    schedule values. Disabling morning weather drops ``morning`` from the
    priority, so the morning screen never shows. When the hub sets a weather
    city it replaces the local ``[location]`` (coordinates and timezone) for the
    forecast. Returns ``cfg`` unchanged when there are no settings (a missing or
    bad control file), so the frame keeps its local config.
    """
    if settings is None:
        return cfg
    schedule = cfg.schedule
    changes: dict = {}
    if settings.quiet_hours is not None:
        changes["quiet_start"], changes["quiet_end"] = settings.quiet_hours
    morning = settings.morning_weather
    if morning is not None:
        changes["morning_weekday"] = morning.weekday
        changes["morning_weekend"] = morning.weekend
        if not morning.enabled:
            changes["priority"] = tuple(
                name for name in schedule.priority if name != "morning"
            )
    result = cfg
    if changes:
        result = replace(result, schedule=replace(schedule, **changes))
    location = settings.weather_location
    if location is not None:
        result = replace(
            result,
            location=replace(
                result.location,
                latitude=location.latitude,
                longitude=location.longitude,
                timezone=location.timezone,
            ),
        )
    return result


def decide(
    now: datetime, cfg: Config, ctx: Context, pin: Pin | None = None
) -> Decision:
    """The screen to show at ``now``.

    With an active ``pin`` the pinned screen wins over the normal schedule, with
    one exception: a live or recently-final game still wins over a pin (the game
    is the one thing already allowed to update overnight). A pin whose screen has
    nothing to show (weather pinned before the first forecast, say) falls through
    to the normal schedule.

    Without a pin the decision walks ``schedule.priority``: only ``morning``,
    ``music``, ``game``, and ``art`` are recognised, and the first whose
    condition holds wins. Nothing matching gives the placeholder.

    One quiet-hours exception for the schedule: during quiet hours every non-game
    screen is held still by ``should_push``, so if the priority winner is not the
    game and a game is on screen, show the game instead, keeping a live game
    moving rather than freezing a non-game screen until morning.
    """
    if pin is not None:
        pinned = _pinned_decision(now, cfg, ctx, pin)
        if pinned is not None:
            return pinned
    chosen = _by_priority(now, cfg, ctx)
    if (
        chosen.screen != "game"
        and in_quiet_hours(now, cfg)
        and ctx.game is not None
        and _game_on_screen(ctx.game, now, cfg)
    ):
        return Decision("game", ("game",) + game_redraw_key(ctx.game))
    return chosen


def _pinned_decision(
    now: datetime, cfg: Config, ctx: Context, pin: Pin
) -> Decision | None:
    """The pinned screen's decision, or ``None`` when it has nothing to show."""
    if ctx.game is not None and _game_on_screen(ctx.game, now, cfg):
        return Decision("game", ("game",) + game_redraw_key(ctx.game))
    if pin.what == "art":
        if ctx.art is not None:
            return Decision("art", _art_key(ctx.art))
        return None
    if pin.what == "weather":
        if ctx.forecast is not None:
            return Decision("weather", _weather_key(now, cfg))
        return None
    if pin.what == "now_playing":
        if ctx.now_playing is not None:
            return Decision(
                "now_playing", _now_playing_key(ctx.now_playing, now, cfg)
            )
        if ctx.art is not None:
            return Decision("art", _art_key(ctx.art))
    return None


def _by_priority(now: datetime, cfg: Config, ctx: Context) -> Decision:
    for name in cfg.schedule.priority:
        if name == "morning":
            if in_morning_window(now, cfg) and ctx.forecast is not None:
                if ctx.now_playing is not None:
                    return Decision("split", _split_key(now, cfg, ctx.now_playing))
                return Decision("weather", _weather_key(now, cfg))
        elif name == "music":
            if ctx.now_playing is not None:
                return Decision(
                    "now_playing", _now_playing_key(ctx.now_playing, now, cfg)
                )
        elif name == "game":
            if ctx.game is not None and _game_on_screen(ctx.game, now, cfg):
                return Decision("game", ("game",) + game_redraw_key(ctx.game))
        elif name == "art":
            if ctx.art is not None:
                return Decision("art", _art_key(ctx.art))
    return Decision("placeholder", ("placeholder",))


def should_push(
    decision: Decision, last_key: tuple | None, now: datetime, cfg: Config
) -> bool:
    """Whether to redraw for ``decision``.

    False when it matches what is already showing (``last_key``). A forced
    decision (an art command) redraws even in quiet hours. Otherwise false during
    quiet hours for every screen but the game. True the rest of the time.
    """
    if decision.redraw_key == last_key:
        return False
    if decision.forced:
        return True
    if in_quiet_hours(now, cfg) and decision.screen != "game":
        return False
    return True


def _game_on_screen(game: GameState, now: datetime, cfg: Config) -> bool:
    """Whether a game should hold the screen: live, or recently final."""
    if game.status == "live":
        return True
    if game.status == "final" and game.ended_at is not None:
        hold = timedelta(minutes=cfg.game.final_hold_minutes)
        return now - game.ended_at <= hold
    return False


def _now_playing_key(np: NowPlaying, now: datetime, cfg: Config) -> tuple:
    """The redraw key for a now-playing track.

    Identity (title, artist, album) and rooms, plus a progress bucket: for a
    ``long_form`` track with a known position, the index of its extrapolated
    position within ``schedule.progress_bucket_seconds`` buckets, else ``None``.
    A paused track freezes the bucket, since ``position_now`` holds the recorded
    position while paused. Music tracks never bucket.
    """
    bucket = None
    if np.kind == "long_form":
        position = np.position_now(now)
        if position is not None:
            bucket = int(position // cfg.schedule.progress_bucket_seconds)
    return ("np", np.title, np.artist, np.album, np.rooms, bucket)


def _weather_key(now: datetime, cfg: Config) -> tuple:
    local = _local(now, cfg)
    return ("wx", local.date(), local.hour, local.minute // 30)


def _split_key(now: datetime, cfg: Config, np: NowPlaying) -> tuple:
    return _weather_key(now, cfg) + _now_playing_key(np, now, cfg)


def _art_key(piece: ArtPiece) -> tuple:
    # The rotation state machine decides when the piece changes, so the redraw
    # key is just the current piece's id: it changes exactly when the piece does.
    return ("art", piece.id)


def _local(now: datetime, cfg: Config) -> datetime:
    tz = cfg.location.timezone
    if tz is not None:
        return now.astimezone(ZoneInfo(tz))
    return now.astimezone()


def _local_minutes(now: datetime, cfg: Config) -> int:
    local = _local(now, cfg)
    return local.hour * 60 + local.minute


def _minutes(hhmm: str) -> int:
    hours, minutes = hhmm.split(":")
    return int(hours) * 60 + int(minutes)
