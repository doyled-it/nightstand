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

import logging
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
from nightstand.screens import art as art_screen
from nightstand.screens import game as game_screen
from nightstand.screens import marker, placeholder, split, weather
from nightstand.screens import now_playing as now_playing_screen
from nightstand.sources import health as health_source
from nightstand.sources import hubnowplaying, mlb
from nightstand.sources import weather as weather_source
from nightstand.sources.coverart import CoverArt
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


class Sources(Protocol):
    """What the loop needs from the outside world, each call degrading to None."""

    def now_playing(self) -> NowPlaying | None: ...
    def cover(self, np: NowPlaying) -> Image.Image | None: ...
    def game(self) -> GameState | None: ...
    def logo(self, team_id: int) -> Image.Image | None: ...
    def forecast(self) -> Forecast | None: ...
    def art(self, now: datetime) -> ArtPiece | None: ...
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
        clock: Callable[[], datetime] = _aware_now,
    ) -> None:
        self._cfg = cfg
        self._client = client
        self._clock = clock
        cache = Path(cache_dir) if cache_dir is not None else cfg.paths.cache
        self._hub_authority = _hub_authority(cfg)
        self._covers = CoverArt(
            client, cache / "covers", f"http://{self._hub_authority}"
        )
        self._logos = LogoCache(client, cache / "logos")
        self._art = ArtLibrary(cfg.art.dir)
        # When each currently-failing source first failed, and when each health
        # URL was first unreachable (for the 15-minute grace).
        self._outages: dict[str, datetime] = {}
        self._unreachable: dict[str, datetime] = {}
        # Throttled external polling: the last good forecast and when it was
        # fetched; the cached schedule result (gamePk, possibly None) with the
        # day and time it was polled; and the last parsed game state.
        self._forecast_cache: Forecast | None = None
        self._forecast_at: datetime | None = None
        self._game_pk: int | None = None
        self._schedule_at: datetime | None = None
        self._schedule_day: date | None = None
        self._game_state: GameState | None = None

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

    def forecast(self) -> Forecast | None:
        location = self._cfg.location
        if location.latitude is None or location.longitude is None:
            # Weather is off until the local config supplies coordinates; this
            # is a clean "nothing to show", not an outage.
            self._clear_outage("forecast")
            return None

        now = self._clock()
        if not forecast_due(now, self._cfg, self._forecast_at):
            # Outside the span, or refreshed recently: serve the cache, no call.
            self._clear_outage("forecast")
            return self._forecast_cache

        value = self._guard("forecast", self._fetch_forecast)
        if value is not None:
            # Advance the timer only on success, so a failed fetch retries next
            # cycle rather than waiting out the TTL.
            self._forecast_cache = value
            self._forecast_at = now
        return self._forecast_cache

    def art(self, now: datetime) -> ArtPiece | None:
        return self._guard(
            "art",
            lambda: self._art.pick(
                now, self._cfg.art.rotate_minutes, self._cfg.art.min_aspect
            ),
        )

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

    def _fetch_forecast(self) -> Forecast | None:
        location = self._cfg.location
        data = weather_source.fetch_forecast(
            self._client, location.latitude, location.longitude, location.timezone
        )
        return weather_source.parse_forecast(data, self._clock())

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
    failed, so a persistent write outage is logged once (on the state change)
    rather than on every cycle.

    ``shown_identity`` is the now-playing identity currently on the panel (or
    ``None`` for nothing playing); ``pending`` is the identity the settle is
    waiting on and when it was first seen, or ``None`` when nothing is pending.
    Together they hold a changed identity off the panel until it has been stable
    for ``settle_seconds``.
    """

    last_key: tuple | None = None
    history_write_failing: bool = False
    shown_identity: TrackKey | None = None
    pending: tuple[TrackKey | None, datetime] | None = None


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


def run_once(
    state: AppState,
    cfg: Config,
    sources: Sources,
    display: Display,
    now: datetime,
    history: ArtHistory | None = None,
) -> bool:
    """Evaluate the rules once; push and return ``True`` only if it redrew."""
    np = sources.now_playing()
    game_state = sources.game()
    forecast = sources.forecast()
    art_piece = sources.art(now)

    ctx = rules.Context(
        now_playing=np, game=game_state, forecast=forecast, art=art_piece
    )
    decision = rules.decide(now, cfg, ctx)

    identity = (np.title, np.artist, np.album) if np is not None else None
    if not _settled(state, identity, now, cfg):
        return False

    health = sources.health()
    marker_state = bool(health.problems)
    key = decision.redraw_key + (marker_state,)

    keyed = replace(decision, redraw_key=key)
    if not rules.should_push(keyed, state.last_key, now, cfg):
        return False

    image = _render(
        decision.screen, cfg, sources, now, np, game_state, forecast, art_piece
    )
    image = marker.apply(image, health)
    display.show(image)
    state.last_key = key
    state.shown_identity = identity
    state.pending = None
    if history is not None and decision.screen == "art" and art_piece is not None:
        # ``pick`` already returned a piece whose image decodes, so an art
        # decision means the art screen is what reached the panel. Record it;
        # the history itself drops a piece that is already current, so a redraw
        # under the same slot (a marker flip, say) does not add a duplicate.
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
        sources = LiveSources(cfg, owned_client, cache_dir=cfg.paths.cache)

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
        return now_playing_screen.render(np, sources.cover(np))
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
