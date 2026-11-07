"""The core loop: gather the sources, decide a screen, redraw when it changed.

``LiveSources`` is the one place that talks to the network. Every source call
is wrapped so a failure logs once when the outage starts, returns ``None``, and
marks the source stale until it answers again; the loop never crashes on a blip.

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
from datetime import datetime
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

import httpx
from PIL import Image

from nightstand import rules
from nightstand.art import ArtLibrary, load_piece_image
from nightstand.config import Config
from nightstand.display import Display, InkyDisplay
from nightstand.models import ArtPiece, Health, NowPlaying
from nightstand.screens import art as art_screen
from nightstand.screens import game as game_screen
from nightstand.screens import marker, placeholder, split, weather
from nightstand.screens import now_playing as now_playing_screen
from nightstand.sources import health as health_source
from nightstand.sources import mlb, snapserver
from nightstand.sources import weather as weather_source
from nightstand.sources.coverart import CoverArt
from nightstand.sources.mlb import GameState, LogoCache
from nightstand.sources.weather import Forecast

logger = logging.getLogger(__name__)

# The live loop falls through to the placeholder only when no screen has
# anything to show, which in practice means the art folder is empty.
_PLACEHOLDER_MESSAGE = "Add art to the share"


def _aware_now() -> datetime:
    return datetime.now().astimezone()


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
    on every failing cycle) and marks that source stale until it answers again.
    ``health().stale_since`` is the earliest such active outage, which is what
    the corner marker reflects.
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
        self._covers = CoverArt(client, cache / "covers")
        self._logos = LogoCache(client, cache / "logos")
        self._art = ArtLibrary(cfg.art.dir)
        # When each currently-failing source first failed, and when each health
        # URL was first unreachable (for the 15-minute grace).
        self._outages: dict[str, datetime] = {}
        self._unreachable: dict[str, datetime] = {}

    def now_playing(self) -> NowPlaying | None:
        return self._guard("now_playing", self._fetch_now_playing)

    def cover(self, np: NowPlaying) -> Image.Image | None:
        return self._guard("cover", lambda: self._covers.for_track(np))

    def game(self) -> GameState | None:
        return self._guard("game", self._fetch_game)

    def logo(self, team_id: int) -> Image.Image | None:
        return self._guard("logo", lambda: self._logos.for_team(team_id))

    def forecast(self) -> Forecast | None:
        location = self._cfg.location
        if location.latitude is None or location.longitude is None:
            # Weather is off until the local config supplies coordinates; this
            # is a clean "nothing to show", not an outage.
            return None
        return self._guard("forecast", self._fetch_forecast)

    def art(self, now: datetime) -> ArtPiece | None:
        return self._guard(
            "art", lambda: self._art.pick(now, self._cfg.art.rotate_minutes)
        )

    def health(self) -> Health:
        problems = health_source.fetch_health(
            self._client, self._cfg.health.urls, self._clock(), self._unreachable
        )
        return Health(problems=problems, stale_since=self._stale_since())

    def _fetch_now_playing(self) -> NowPlaying | None:
        status = snapserver.fetch_status(
            self._client, self._cfg.hub.host, self._cfg.hub.snapserver_port
        )
        return snapserver.parse_now_playing(status)

    def _fetch_game(self) -> GameState | None:
        pk = mlb.find_game(
            self._client, self._cfg.game.team_id, self._local_now().date()
        )
        if pk is None:
            return None
        return mlb.parse_game(mlb.fetch_feed(self._client, pk))

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

    def _stale_since(self) -> datetime | None:
        if not self._outages:
            return None
        return min(self._outages.values())

    def _local_now(self) -> datetime:
        now = self._clock()
        tz = self._cfg.location.timezone
        if tz is not None:
            return now.astimezone(ZoneInfo(tz))
        return now.astimezone()


@dataclass
class AppState:
    """What the loop remembers between cycles: the last pushed redraw key.

    The key already folds in the marker state, so a change in health alone is
    enough to make the next permitted redraw repaint. (Outage tracking lives in
    ``LiveSources``, which is where the failing source calls are caught.)
    """

    last_key: tuple | None = None


def run_once(
    state: AppState,
    cfg: Config,
    sources: Sources,
    display: Display,
    now: datetime,
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

    health = sources.health()
    marker_state = (bool(health.problems), health.stale_since)
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
    return True


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
    try:
        while True:
            run_once(state, cfg, sources, display, clock())
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
