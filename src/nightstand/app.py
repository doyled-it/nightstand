"""The core loop: gather the sources, decide a screen, redraw when it changed.

``LiveSources`` is the one place that talks to the network. Every source call
is wrapped so a failure logs once when the outage starts, returns ``None``, and
retries until it answers again; the loop never crashes on a blip.

``run_once`` evaluates the rules for a single moment: it reads each source,
asks ``rules.decide`` what to show, folds the health-marker state into the
redraw key, and pushes to the display only when ``rules.should_push`` allows.
``run_forever`` repeats that every ``check_seconds``. The marker never forces a
redraw: it rides along with whatever redraw the rules already permit.
"""

from __future__ import annotations

import io
import json
import logging
import os
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

import httpx
from PIL import Image

from nightstand import rules
from nightstand.art import ArtLibrary, load_piece_image
from nightstand.config import Config
from nightstand.display import Display, InkyDisplay
from nightstand.history import ArtHistory
from nightstand.models import ArtPiece, Health, NowPlaying
from nightstand.palette import simulate_panel
from nightstand.rotation import Rotation
from nightstand.screens import art as art_screen
from nightstand.screens import game as game_screen
from nightstand.screens import marker, placeholder, split, weather
from nightstand.screens import now_playing as now_playing_screen
from nightstand.sources import artcontrol, framecontrol, hubnowplaying, mlb
from nightstand.sources import health as health_source
from nightstand.sources import weather as weather_source
from nightstand.sources.coverart import CoverArt
from nightstand.sources.framecontrol import FrameControl
from nightstand.sources.mlb import GameState, LogoCache
from nightstand.sources.weather import Forecast

logger = logging.getLogger(__name__)

# The live loop falls through to the placeholder only when no screen has
# anything to show, which in practice means the art folder is empty.
_PLACEHOLDER_MESSAGE = "Add art to the share"

# External-source polling policy. The hub's now-playing.json is a small LAN file
# read every cycle, but the MLB and Open-Meteo calls go out to the internet, so
# they are throttled: the forecast only around the morning window, the MLB
# schedule a few times an hour, and the live feed only while a game is in
# progress.
_FORECAST_TTL = timedelta(minutes=30)
_FORECAST_LEAD = timedelta(minutes=30)
_SCHEDULE_TTL = timedelta(minutes=15)


def _aware_now() -> datetime:
    return datetime.now().astimezone()


def _hub_authority(cfg: Config) -> str:
    """The hub's HTTP authority, omitting the port when it is the default 80."""
    host = cfg.hub.host
    port = cfg.hub.http_port
    return host if port == 80 else f"{host}:{port}"


def _local(now: datetime, cfg: Config) -> datetime:
    """``now`` in the configured timezone, or the system zone when unset."""
    tz = cfg.location.timezone
    if tz is not None:
        return now.astimezone(ZoneInfo(tz))
    return now.astimezone()


def _minutes(hhmm: str) -> int:
    hours, mins = hhmm.split(":")
    return int(hours) * 60 + int(mins)


def _morning_window(now: datetime, cfg: Config) -> tuple[int, int]:
    """The morning window for ``now``'s local day, as minutes past midnight."""
    local = _local(now, cfg)
    if local.weekday() < 5:
        start, end = cfg.schedule.morning_weekday
    else:
        start, end = cfg.schedule.morning_weekend
    return _minutes(start), _minutes(end)


def forecast_due(now: datetime, cfg: Config, last_at: datetime | None) -> bool:
    """Whether to fetch the forecast at ``now``.

    Only inside the morning window or the 30 minutes before it starts, and then
    at most once every 30 minutes (measured from the last successful fetch).
    Outside that span the caller uses its cached value and makes no request.
    """
    start, end = _morning_window(now, cfg)
    local = _local(now, cfg)
    current = local.hour * 60 + local.minute
    lead = int(_FORECAST_LEAD.total_seconds() // 60)
    if not (start - lead <= current < end):
        return False
    return last_at is None or now - last_at >= _FORECAST_TTL


def schedule_due(
    now: datetime,
    day: date,
    last_at: datetime | None,
    last_day: date | None,
) -> bool:
    """Whether to call ``find_game`` at ``now``.

    At most once every 15 minutes, but always once when the local day rolls
    over so a finished game does not linger on screen into the next day.
    """
    if last_at is None or last_day != day:
        return True
    return now - last_at >= _SCHEDULE_TTL


def feed_due(state: GameState | None) -> bool:
    """Whether to fetch the live feed, given the last known game state.

    Fetch when there is no state yet (a game was just found) and every cycle
    while it is live. Once it is final, stop: the cached final state is held.
    """
    if state is None:
        return True
    return state.status == "live"


@dataclass(frozen=True)
class ArtView:
    """The art piece to show now, and whether a command just forced the change.

    ``forced`` is ``True`` only on the cycle a command (next, previous, show)
    changed the piece, so the loop can redraw immediately even in quiet hours.
    """

    piece: ArtPiece | None
    forced: bool = False


class Sources(Protocol):
    """What the loop needs from the outside world, each call degrading to None."""

    def frame_control(self) -> FrameControl | None: ...
    def now_playing(self) -> NowPlaying | None: ...
    def cover(self, np: NowPlaying) -> Image.Image | None: ...
    def game(self) -> GameState | None: ...
    def logo(self, team_id: int) -> Image.Image | None: ...
    def forecast(
        self,
        now: datetime | None = None,
        cfg: Config | None = None,
        *,
        pinned_weather: bool = False,
    ) -> Forecast | None: ...
    def art(self, now: datetime, pinned_piece: str | None = None) -> ArtView: ...
    def commit_art(self, now: datetime) -> None: ...
    def health(self) -> Health: ...


class LiveSources:
    """The real sources, each wrapped to degrade to ``None`` on failure.

    An exception in a source call logs one warning when the outage begins (not
    on every failing cycle) and is retried until it answers again. A source
    outage degrades that source to ``None``; it does not draw the corner marker,
    which reflects machine-health problems only.
    """

    def __init__(
        self,
        cfg: Config,
        client: httpx.Client,
        *,
        cache_dir: Path | None = None,
        state_dir: Path | None = None,
        clock: Callable[[], datetime] = _aware_now,
    ) -> None:
        self._cfg = cfg
        self._client = client
        self._clock = clock
        cache = Path(cache_dir) if cache_dir is not None else cfg.paths.cache
        state = Path(state_dir) if state_dir is not None else cfg.paths.state
        self._hub_authority = _hub_authority(cfg)
        self._covers = CoverArt(
            client, cache / "covers", f"http://{self._hub_authority}"
        )
        self._logos = LogoCache(client, cache / "logos")
        self._art = ArtLibrary(cfg.art.dir)
        self._rotation = Rotation(state / "rotation.json", self._clock)
        # When each currently-failing source first failed, and when each health
        # URL was first unreachable (for the 15-minute grace).
        self._outages: dict[str, datetime] = {}
        self._unreachable: dict[str, datetime] = {}
        # Throttled external polling: the last good forecast and when it was
        # fetched; the cached schedule result (gamePk, possibly None) with the
        # day and time it was polled; and the last parsed game state.
        self._forecast_cache: Forecast | None = None
        self._forecast_at: datetime | None = None
        # Whether the "weather on but no location" warning has already been
        # logged for the current state, so it fires once per state change.
        self._no_location_warned = False
        self._game_pk: int | None = None
        self._schedule_at: datetime | None = None
        self._schedule_day: date | None = None
        self._game_state: GameState | None = None

    def frame_control(self) -> FrameControl | None:
        # fetch_control already swallows its own HTTP errors and returns None for
        # a missing or bad file, so the guard here only catches the unexpected and
        # keeps a surprise from stopping the loop.
        return self._guard(
            "frame_control",
            lambda: framecontrol.fetch_control(self._client, self._hub_authority),
        )

    def now_playing(self) -> NowPlaying | None:
        return self._guard("now_playing", self._fetch_now_playing)

    def cover(self, np: NowPlaying) -> Image.Image | None:
        return self._guard("cover", lambda: self._covers.for_track(np))

    def game(self) -> GameState | None:
        now = self._clock()
        day = _local(now, self._cfg).date()

        if schedule_due(now, day, self._schedule_at, self._schedule_day):
            pk = self._guard(
                "game",
                lambda: mlb.find_game(self._client, self._cfg.game.team_id, day),
            )
            if "game" not in self._outages:
                # A successful poll (``pk`` may legitimately be None for no game
                # today). On failure we keep the old pk and retry next cycle.
                if pk != self._game_pk:
                    self._game_state = None
                self._game_pk = pk
                self._schedule_at = now
                self._schedule_day = day

        if self._game_pk is None:
            self._clear_outage("game")
            return None

        if feed_due(self._game_state):
            state = self._guard(
                "game",
                lambda: mlb.parse_game(mlb.fetch_feed(self._client, self._game_pk)),
            )
            if state is not None:
                self._game_state = state
        else:
            # Final (or held): no feed call, so no outage to carry.
            self._clear_outage("game")
        return self._game_state

    def logo(self, team_id: int) -> Image.Image | None:
        # cover and logo already swallow their own network errors and return
        # None, so the guard here never fires: a missing cover or logo is a
        # clean "nothing to draw" and deliberately does not feed the marker.
        return self._guard("logo", lambda: self._logos.for_team(team_id))

    def forecast(
        self,
        now: datetime | None = None,
        cfg: Config | None = None,
        *,
        pinned_weather: bool = False,
    ) -> Forecast | None:
        cfg = cfg if cfg is not None else self._cfg
        # The hub's weather city, when set, has already replaced the local
        # [location] in the effective config the caller passes in; without one
        # this is the local [location].
        location = cfg.location
        if location.latitude is None or location.longitude is None:
            # No location from the hub or the local config. If morning weather is
            # on, warn once per state change; the weather simply stays skipped,
            # a clean "nothing to show" rather than an outage.
            self._warn_no_location(cfg)
            self._clear_outage("forecast")
            return None
        # A location is available again: the next time it goes missing warns anew.
        self._no_location_warned = False

        now = now if now is not None else self._clock()
        if pinned_weather:
            # While the weather is pinned the forecast must stay current all day,
            # not only around the morning window, so refresh it every 30 minutes
            # regardless of the clock.
            due = self._forecast_at is None or now - self._forecast_at >= _FORECAST_TTL
        else:
            due = forecast_due(now, cfg, self._forecast_at)
        if not due:
            # Outside the span, or refreshed recently: serve the cache, no call.
            self._clear_outage("forecast")
            return self._forecast_cache

        value = self._guard("forecast", lambda: self._fetch_forecast(location))
        if value is not None:
            # Advance the timer only on success, so a failed fetch retries next
            # cycle rather than waiting out the TTL.
            self._forecast_cache = value
            self._forecast_at = now
        return self._forecast_cache

    def art(self, now: datetime, pinned_piece: str | None = None) -> ArtView:
        view = self._guard("art", lambda: self._compute_art(now, pinned_piece))
        return view if isinstance(view, ArtView) else ArtView(piece=None)

    def _compute_art(self, now: datetime, pinned_piece: str | None) -> ArtView:
        """Follow the hub: read the control file, apply a command, pick a piece.

        The hub's settings override the local rotation interval; a missing or bad
        control file falls back to the local config. Only enabled folders are on
        the device (the sync mirrors just those), so the eligible set is the local
        library filtered by the landscape rule.

        While the art is pinned (``pinned_piece`` set) the frame holds that piece
        and the rotation does not advance; a pinned piece that has been deleted
        from the library falls back to the normal schedule (Review Focus 4).
        """
        control = artcontrol.fetch_control(self._client, self._hub_authority)
        # The hub's cutoff is the one source of truth for every frame; the local
        # config is only the fallback when the hub does not set one.
        min_aspect = self._cfg.art.min_aspect
        if control is not None and control.min_aspect is not None:
            min_aspect = control.min_aspect
        eligible = self._art.eligible(min_aspect)

        if pinned_piece is not None:
            pinned = {piece.id: piece for piece in eligible}.get(pinned_piece)
            if pinned is not None:
                # Hold the pinned piece: the rotation does not advance and a
                # command is not applied while the art is pinned.
                return ArtView(piece=pinned, forced=False)
            # The pinned piece is gone; fall through to the normal schedule.

        interval = (
            control.rotate_minutes
            if control is not None
            else self._cfg.art.rotate_minutes
        )
        forced = False
        if control is not None:
            forced = self._rotation.apply(control, eligible, now)
        piece = self._rotation.current(eligible, now, interval)
        return ArtView(piece=piece, forced=forced)

    def commit_art(self, now: datetime) -> None:
        # Confirm a scheduled advance only when the art screen actually reached
        # the panel, so "previous" walks what was shown (final review
        # Important-1). A command already advanced in ``apply``; this is then a
        # no-op. Never let a failure here stop the loop.
        self._guard("commit_art", lambda: self._rotation.commit(now))

    def health(self) -> Health:
        # Guarded like the other sources so a health-poll failure degrades to
        # "no known problems" and can never stop the loop.
        problems = self._guard(
            "health",
            lambda: health_source.fetch_health(
                self._client, self._cfg.health.urls, self._clock(), self._unreachable
            ),
        )
        return Health(problems=problems or ())

    def _fetch_now_playing(self) -> NowPlaying | None:
        doc = hubnowplaying.fetch_now_playing(self._client, self._hub_authority)
        return hubnowplaying.parse_now_playing(doc)

    def _fetch_forecast(self, location) -> Forecast | None:
        data = weather_source.fetch_forecast(
            self._client, location.latitude, location.longitude, location.timezone
        )
        # Parse against ``now`` in the location's timezone, so the forecast's
        # naive local times read in the place being shown, not the machine's
        # zone. A location with no timezone keeps the system zone.
        return weather_source.parse_forecast(data, self._forecast_now(location))

    def _forecast_now(self, location) -> datetime:
        now = self._clock()
        if location.timezone is not None:
            return now.astimezone(ZoneInfo(location.timezone))
        return now

    def _warn_no_location(self, cfg: Config) -> None:
        """Warn once when morning weather is on but no location is set.

        "Weather is on" means the effective config still carries the ``morning``
        screen in its priority (the hub has not disabled morning weather, or
        there is no hub control and the local default keeps it). When weather is
        off there is nothing to nag about, so the warning state resets.
        """
        weather_on = "morning" in cfg.schedule.priority
        if not weather_on:
            self._no_location_warned = False
            return
        if not self._no_location_warned:
            self._no_location_warned = True
            logger.warning(
                "weather is on but no location is set; "
                "set a weather city in the room picker Settings"
            )

    def _guard(self, name: str, call: Callable[[], object]):
        try:
            value = call()
        except Exception as error:  # a source failure must never crash the loop
            if name not in self._outages:
                self._outages[name] = self._clock()
                logger.warning("source %s failed: %s", name, error)
            return None
        if self._outages.pop(name, None) is not None:
            logger.info("source %s recovered", name)
        return value

    def _clear_outage(self, name: str) -> None:
        # A source we deliberately did not poll this cycle (cache hit, out of
        # window, no game) is not failing, so it leaves no lingering outage.
        self._outages.pop(name, None)


# The now-playing identity the settle holds on: title, artist, album. A track
# with a title but no artist carries an empty-string artist; album is None when
# absent. ``None`` (not this tuple) means nothing is playing.
TrackKey = tuple[str, str, str | None]


@dataclass
class AppState:
    """What the loop remembers between cycles: the last pushed redraw key.

    The key already folds in the marker state, so a change in health alone is
    enough to make the next permitted redraw repaint. (Outage tracking lives in
    ``LiveSources``, which is where the failing source calls are caught.)

    ``history_write_failing`` remembers whether the last art-history write
    failed, and ``mirror_write_failing`` the same for the frame mirror, so a
    persistent write outage is logged once (on the state change) rather than on
    every cycle.

    ``shown_identity`` is the now-playing identity currently on the panel (or
    ``None`` for nothing playing); ``pending`` is the identity the settle is
    waiting on and when it was first seen, or ``None`` when nothing is pending.
    Together they hold a changed identity off the panel until it has been stable
    for ``settle_seconds``.

    ``last_held`` remembers the last held redraw already logged (its reason and
    the changed decision), so a hold is logged once when it starts rather than on
    every 10 s cycle; a push clears it so the next hold logs afresh.

    ``pin_seq`` is the sequence number of the last pin the loop acted on, and
    ``pin_seen`` records that the loop has seen at least one control: the first
    control is adopted as a baseline without forcing a redraw (the startup grace),
    so a frame that boots with a pin already set does not wake the panel at night.
    A later pin with a higher ``seq`` forces one redraw even in quiet hours.
    """

    last_key: tuple | None = None
    history_write_failing: bool = False
    mirror_write_failing: bool = False
    shown_identity: TrackKey | None = None
    pending: tuple[TrackKey | None, datetime] | None = None
    last_held: tuple | None = None
    pin_seq: int | None = None
    pin_seen: bool = False
    pin_eval_failing: bool = False


def _settled(
    state: AppState, identity: TrackKey | None, now: datetime, cfg: Config
) -> bool:
    """Whether the now-playing identity has held long enough to redraw.

    An identity equal to what is already shown is settled at once, so bucket and
    room changes within the same track never wait. A changed identity starts (or
    continues) a settle timer and is held until it has been seen continuously for
    ``settle_seconds``; a different identity arriving mid-wait restarts the timer.
    """
    if identity == state.shown_identity:
        state.pending = None
        return True
    if state.pending is None or state.pending[0] != identity:
        state.pending = (identity, now)
        return False
    elapsed = (now - state.pending[1]).total_seconds()
    return elapsed >= cfg.schedule.settle_seconds


def _push_detail(
    screen: str, np: NowPlaying | None, art_piece: ArtPiece | None
) -> str:
    """A short identifier for the pushed screen: a piece id or a track title.

    Returns a leading-space suffix for the push log (``" Photos/beach.jpg"``) or
    an empty string when the screen has no obvious single identifier. Only data
    the screens already show reaches the log.
    """
    if screen == "art" and art_piece is not None:
        return f" {art_piece.id or art_piece.title}"
    if screen in ("now_playing", "split") and np is not None:
        return f" {np.title}"
    return ""


def _hold_signature(
    screen: str,
    np: NowPlaying | None,
    art_piece: ArtPiece | None,
    identity: TrackKey | None,
) -> object:
    """The stable identity of a held decision, for deduping the held log.

    The art piece id or the now-playing identity, deliberately without the
    progress bucket or health-marker bit that also ride in the redraw key. A
    hold on the same piece or track is then logged once even as its bucket
    advances or the marker flips through the night.
    """
    if screen == "art" and art_piece is not None:
        return art_piece.id or art_piece.title
    if screen in ("now_playing", "split"):
        return identity
    return None


def _log_held(
    state: AppState, reason: str, screen: str, signature: object
) -> None:
    """Log a held redraw once, when its reason, screen, or decision changes.

    ``signature`` identifies the held decision (see ``_hold_signature``). While
    it, ``reason``, and ``screen`` all stay the same the hold is not re-logged,
    so a steady hold does not spam the log every cycle.
    """
    held = (reason, screen, signature)
    if state.last_held == held:
        return
    state.last_held = held
    logger.info("redraw held: %s (%s)", reason, screen)


def _safe_active_pin(state: AppState, control: FrameControl | None, now: datetime):
    """The active pin, degrading to no pin if evaluating it raises.

    The parser rejects a naive ``until`` already, but this is a second line of
    defense for Review Focus 1: a frame-control problem must never stop the loop.
    Any exception here (a naive stamp that slipped through, say, which would raise
    on the aware/naive comparison) degrades to no pin, logged once per outage like
    the other sources.
    """
    try:
        pin = rules.active_pin(control, now)
    except Exception as error:
        if not state.pin_eval_failing:
            state.pin_eval_failing = True
            logger.warning("pin evaluation failed, ignoring pin: %s", error)
        return None
    if state.pin_eval_failing:
        state.pin_eval_failing = False
        logger.info("pin evaluation recovered")
    return pin


def run_once(
    state: AppState,
    cfg: Config,
    sources: Sources,
    display: Display,
    now: datetime,
    history: ArtHistory | None = None,
) -> bool:
    """Evaluate the rules once; push and return ``True`` only if it redrew."""
    control = sources.frame_control()
    settings = control.settings if control is not None else None
    pin = _safe_active_pin(state, control, now)
    if control is not None and not state.pin_seen:
        # Startup grace: the first control the loop actually sees is a baseline,
        # not a change, so a reboot into an existing pin does not wake the panel
        # at night. A cycle with no control (the hub briefly unreachable) does not
        # spend the grace. A later pin with a higher seq forces one redraw (below).
        state.pin_seen = True
        if pin is not None:
            state.pin_seq = pin.seq
    # The hub's quiet hours and morning window override the local config while a
    # control is present; without one the frame keeps its local config.
    cfg = rules.effective_config(cfg, settings)
    kinds = (
        settings.now_playing_kinds
        if settings is not None
        else framecontrol.DEFAULT_KINDS
    )

    np_raw = sources.now_playing()
    np = rules.now_playing_for_frame(np_raw, kinds, pin)
    game_state = sources.game()
    forecast = sources.forecast(
        now, cfg, pinned_weather=pin is not None and pin.what == "weather"
    )
    pinned_piece = pin.piece if pin is not None and pin.what == "art" else None
    art_view = sources.art(now, pinned_piece)
    art_piece = art_view.piece

    ctx = rules.Context(
        now_playing=np, game=game_state, forecast=forecast, art=art_piece
    )
    decision = rules.decide(now, cfg, ctx, pin)
    if art_view.forced and decision.screen == "art":
        # A command (next, previous, show) just changed the piece: redraw now,
        # even in quiet hours.
        decision = replace(decision, forced=True)

    identity = (np.title, np.artist, np.album) if np is not None else None
    if not _settled(state, identity, now, cfg):
        # A changed identity is waiting out the settle, not yet on the panel.
        _log_held(state, "settling", decision.screen, identity)
        return False

    if pin is not None and (state.pin_seq is None or pin.seq > state.pin_seq):
        # A new pin (a higher seq than the last one acted on) forces one redraw,
        # even in quiet hours. This is past the settle gate, so a held cycle never
        # consumes the pin's one forced redraw. An unchanged pin, an expired pin,
        # and an unpin do not force: they return to the schedule at the next
        # permitted redraw.
        state.pin_seq = pin.seq
        decision = replace(decision, forced=True)

    health = sources.health()
    marker_state = bool(health.problems)
    key = decision.redraw_key + (marker_state,)

    keyed = replace(decision, redraw_key=key)
    if not rules.should_push(keyed, state.last_key, now, cfg):
        # should_push returns False either because nothing changed (key matches
        # what is shown, nothing to log) or because a changed, unforced decision
        # is held by quiet hours: that is the only other reason it declines here.
        if key != state.last_key:
            signature = _hold_signature(decision.screen, np, art_piece, identity)
            _log_held(state, "quiet hours", decision.screen, signature)
        else:
            # Nothing is held this cycle, so forget any earlier hold: a later
            # recurrence of it is a fresh hold and logs again.
            state.last_held = None
        return False

    image = _render(
        decision.screen, cfg, sources, now, np, game_state, forecast, art_piece
    )
    image = marker.apply(image, health)
    display.show(image)
    logger.info(
        "pushed %s%s (%s)",
        decision.screen,
        _push_detail(decision.screen, np, art_piece),
        "forced" if decision.forced else "scheduled",
    )
    _write_mirror(state, cfg.paths.public, image, decision.screen, now)
    state.last_key = key
    state.last_held = None
    state.shown_identity = identity
    state.pending = None
    if decision.screen == "art" and art_piece is not None:
        # The art screen is what reached the panel (its image already decoded),
        # so confirm any scheduled advance now: the rotation and its back stack
        # track what was shown, not the clock (final review Important-1). A
        # command already advanced in ``apply``, so this is then a no-op.
        sources.commit_art(now)
        if history is not None:
            # The history itself drops a piece that is already current, so a
            # redraw under the same slot (a marker flip, say) adds no duplicate.
            _record_history(state, history, art_piece, now)
    return True


def _record_history(
    state: AppState, history: ArtHistory, piece: ArtPiece, now: datetime
) -> None:
    """Record ``piece`` in the art history, surviving any write failure.

    The display is already pushed by the time this runs. A write to the public
    directory can still fail (it may not be writable yet on a fresh boot), and
    that failure must never stop the loop or systemd will restart it into an
    endless e-ink repaint. Swallow the error and log it once per outage.
    """
    try:
        history.record(piece, now)
    except OSError as error:
        if not state.history_write_failing:
            state.history_write_failing = True
            logger.warning("art history write failed: %s", error)
        return
    if state.history_write_failing:
        state.history_write_failing = False
        logger.info("art history write recovered")


#: The mode the published mirror files carry, so nginx can serve them.
_PUBLIC_FILE_MODE = 0o644


def _write_mirror(
    state: AppState, public: Path, image: Image.Image, screen: str, now: datetime
) -> None:
    """Publish the pushed image as the frame mirror, surviving any write failure.

    After a successful push the frame writes ``frame.png`` (the pushed image
    through ``simulate_panel``, so it reads like the reflective panel) and
    ``frame.json`` (``{"updated_at", "screen"}``) into the public directory the
    hub mirrors onto the room-picker page. The write can fail (the directory may
    not be writable yet on a fresh boot); as with the art history that must never
    stop the loop, so the error is swallowed and logged once per outage.
    """
    try:
        _write_mirror_files(public, image, screen, now)
    except OSError as error:
        if not state.mirror_write_failing:
            state.mirror_write_failing = True
            logger.warning("frame mirror write failed: %s", error)
        return
    if state.mirror_write_failing:
        state.mirror_write_failing = False
        logger.info("frame mirror write recovered")


def _write_mirror_files(
    public: Path, image: Image.Image, screen: str, now: datetime
) -> None:
    """Atomically write ``frame.png`` and ``frame.json`` into ``public``."""
    public.mkdir(parents=True, exist_ok=True)
    buffer = io.BytesIO()
    simulate_panel(image).save(buffer, format="PNG")
    _atomic_write(public / "frame.png", buffer.getvalue())
    meta = json.dumps({"updated_at": now.isoformat(), "screen": screen}, indent=2)
    _atomic_write(public / "frame.json", (meta + "\n").encode("utf-8"))


def _atomic_write(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` via a temp file and rename, chmod 0644."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.chmod(tmp, _PUBLIC_FILE_MODE)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def run_forever(
    cfg: Config,
    *,
    sources: Sources | None = None,
    display: Display | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], datetime] = _aware_now,
) -> None:
    """Run the loop forever, sleeping ``check_seconds`` between evaluations."""
    if display is None:
        display = InkyDisplay()
    owned_client: httpx.Client | None = None
    if sources is None:
        owned_client = httpx.Client()
        sources = LiveSources(
            cfg, owned_client, cache_dir=cfg.paths.cache, state_dir=cfg.paths.state
        )

    state = AppState()
    history = ArtHistory(cfg.paths.public / "art-history.json")
    try:
        while True:
            run_once(state, cfg, sources, display, clock(), history)
            sleep(cfg.schedule.check_seconds)
    finally:
        if owned_client is not None:
            owned_client.close()


def _render(
    screen: str,
    cfg: Config,
    sources: Sources,
    now: datetime,
    np: NowPlaying | None,
    game_state: GameState | None,
    forecast: Forecast | None,
    art_piece: ArtPiece | None,
) -> Image.Image:
    if screen == "now_playing":
        return now_playing_screen.render(np, sources.cover(np), now)
    if screen == "split":
        return split.render(forecast, np, sources.cover(np), now)
    if screen == "weather":
        return weather.render(forecast, now)
    if screen == "game":
        away = sources.logo(game_state.away.id)
        home = sources.logo(game_state.home.id)
        return game_screen.render(game_state, away, home)
    if screen == "art":
        image = load_piece_image(art_piece)
        if image is not None:
            return art_screen.render(art_piece, image)
    return placeholder.render(_PLACEHOLDER_MESSAGE)
