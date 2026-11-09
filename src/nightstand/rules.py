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

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from nightstand.config import Config
from nightstand.models import ArtPiece, NowPlaying
from nightstand.sources.mlb import GameState, game_redraw_key
from nightstand.sources.weather import Forecast

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


def decide(now: datetime, cfg: Config, ctx: Context) -> Decision:
    """The screen to show at ``now``, by walking ``schedule.priority``.

    Unknown names in ``schedule.priority`` are ignored: only ``morning``,
    ``music``, ``game``, and ``art`` are recognised, and the first whose
    condition holds wins. Nothing matching gives the placeholder.

    One quiet-hours exception: during quiet hours every non-game screen is held
    still by ``should_push``, so if the priority winner is not the game and a
    game is on screen (live, or recently final), show the game instead. The game
    is the one screen allowed to update overnight, so this keeps a live game
    moving rather than freezing a non-game screen until morning. Outside quiet
    hours the configured priority is unchanged.
    """
    chosen = _by_priority(now, cfg, ctx)
    if (
        chosen.screen != "game"
        and in_quiet_hours(now, cfg)
        and ctx.game is not None
        and _game_on_screen(ctx.game, now, cfg)
    ):
        return Decision("game", ("game",) + game_redraw_key(ctx.game))
    return chosen


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
