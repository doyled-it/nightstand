"""Padres game state from the MLB Stats API.

The public MLB Stats API needs no key. ``find_game`` asks the schedule which
game to follow on a given day, ``fetch_feed`` pulls that game's live feed, and
``parse_game`` reduces the feed to the one ``GameState`` the screen draws: the
score, the half-inning, and a single summary line.

The screen redraws only on big moments, not every pitch. ``game_redraw_key``
captures exactly those: a run scoring, a half-inning completing, or the game
ending. The middle and the bottom of an inning share a key, so the frame redraws
once when a half ends, not twice.

Team logos are fetched from MLB's CDN and cached on the device by ``LogoCache``.
They are never committed; reduced to the six panel colors at render time.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass, replace
from datetime import date, datetime
from pathlib import Path
from typing import Literal

import httpx
from PIL import Image, UnidentifiedImageError

logger = logging.getLogger(__name__)

_SCHEDULE = "https://statsapi.mlb.com/api/v1/schedule"
_FEED = "https://statsapi.mlb.com/api/v1.1/game/{pk}/feed/live"
_TIMEOUT = 10.0

# MLB serves a ready-made PNG spot logo, so no SVG rasterizer is needed.
_LOGO_URL = "https://midfield.mlbstatic.com/v1/team/{team_id}/spots/{size}"
LOGO_SIZE = 150
# A logo is a few KB; cap the download so a broken or hostile source cannot
# stream an unbounded body onto the device.
_MAX_LOGO_BYTES = 2 * 1024 * 1024

Status = Literal["live", "final"]
InningState = Literal["Top", "Middle", "Bottom", "End"]

# How many half-innings are complete at the start of each inning state. Top: the
# inning has not started, so none beyond prior innings. Middle and Bottom: the
# top is done. End: both halves are done.
_HALF_OFFSET: dict[str, int] = {"Top": 0, "Middle": 1, "Bottom": 1, "End": 2}


@dataclass(frozen=True)
class Team:
    """One side of a game: its MLB id, short code, and run total."""

    id: int
    abbrev: str
    runs: int


@dataclass(frozen=True)
class GameState:
    """The game as of the last redraw: score, half-inning, and summary line.

    ``summary`` is the one line along the bottom of the screen. ``ended_at`` is
    when the game finished, used to hold the final score on screen for a while
    after; it is ``None`` while the game is live.
    """

    game_pk: int
    status: Status
    away: Team
    home: Team
    inning: int
    inning_state: InningState
    summary: str
    ended_at: datetime | None


def find_game(client: httpx.Client, team_id: int, day: date) -> int | None:
    """The gamePk to follow for ``team_id`` on ``day``, or ``None``.

    Prefers a game that is live; otherwise the latest final. A postponed or
    suspended game counts as no game at all.
    """
    response = client.get(
        _SCHEDULE,
        params={"sportId": 1, "teamId": team_id, "date": day.isoformat()},
        timeout=_TIMEOUT,
    )
    response.raise_for_status()
    games = [
        game
        for date_entry in response.json().get("dates", [])
        for game in date_entry.get("games", [])
        if not _unplayable(game)
    ]

    live = [g for g in games if _abstract_state(g) == "Live"]
    if live:
        return _latest(live)
    finals = [g for g in games if _abstract_state(g) == "Final"]
    if finals:
        return _latest(finals)
    return None


def fetch_feed(client: httpx.Client, game_pk: int) -> dict:
    """Fetch the live feed for ``game_pk``."""
    response = client.get(_FEED.format(pk=game_pk), timeout=_TIMEOUT)
    response.raise_for_status()
    return response.json()


def parse_game(feed: dict) -> GameState | None:
    """Reduce a live feed to a ``GameState``, or ``None`` before first pitch."""
    game_data = feed.get("gameData", {})
    abstract = game_data.get("status", {}).get("abstractGameState")
    if abstract == "Final":
        status: Status = "final"
    elif abstract == "Live":
        status = "live"
    else:
        # Preview (or anything not yet underway): nothing to show.
        return None

    teams = game_data.get("teams", {})
    line_teams = feed.get("liveData", {}).get("linescore", {}).get("teams", {})
    away = _team(teams.get("away", {}), line_teams.get("away", {}))
    home = _team(teams.get("home", {}), line_teams.get("home", {}))

    linescore = feed.get("liveData", {}).get("linescore", {})
    game = GameState(
        game_pk=feed.get("gamePk"),
        status=status,
        away=away,
        home=home,
        inning=linescore.get("currentInning") or 0,
        inning_state=linescore.get("inningState") or "Top",
        summary="",
        ended_at=_ended_at(feed) if status == "final" else None,
    )
    return replace(game, summary=summarize(feed, game))


def summarize(feed: dict, game: GameState) -> str:
    """The one-line summary for ``game``: the result, the last half, or a play."""
    if game.status == "final":
        if game.away.runs >= game.home.runs:
            winner, won, lost = game.away.abbrev, game.away.runs, game.home.runs
        else:
            winner, won, lost = game.home.abbrev, game.home.runs, game.away.runs
        return f"Final: {winner} win {won}-{lost}"

    if game.inning_state in ("Middle", "End"):
        top = game.inning_state == "Middle"
        side = "away" if top else "home"
        abbrev = game.away.abbrev if top else game.home.abbrev
        half = "top" if top else "bottom"
        runs = _half_runs(feed, game.inning, side)
        if runs > 0:
            return f"{abbrev} scored {runs} in the {half} {ordinal(game.inning)}"
        return f"No runs in the {half} {ordinal(game.inning)}"

    return _current_half_scoring_play(feed, game)


def state_label(game: GameState) -> str:
    """The short state label: ``"Top 7th"``, ``"End 7th"``, or ``"Final"``."""
    if game.status == "final":
        return "Final"
    return f"{game.inning_state} {ordinal(game.inning)}"


def completed_halves(game: GameState) -> int:
    """How many half-innings have completed by ``game``'s state."""
    return 2 * (game.inning - 1) + _HALF_OFFSET[game.inning_state]


def game_redraw_key(game: GameState) -> tuple:
    """What a redraw depends on: score, half completed, and whether it ended.

    The middle and the bottom of an inning share a key, so a completed half
    redraws the frame once, not twice.
    """
    return (
        game.game_pk,
        game.away.runs,
        game.home.runs,
        completed_halves(game),
        game.status,
    )


def ordinal(n: int) -> str:
    """``1`` to ``"1st"``, ``2`` to ``"2nd"``, ``11`` to ``"11th"``, and so on."""
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


class LogoCache:
    """Fetch and cache a team's logo as a PNG under ``cache_dir``."""

    def __init__(
        self, client: httpx.Client, cache_dir: Path, *, size: int = LOGO_SIZE
    ) -> None:
        self._client = client
        self._cache_dir = Path(cache_dir)
        self._size = size

    def for_team(self, team_id: int) -> Image.Image | None:
        """The logo for ``team_id``, from the cache or the CDN, or ``None``."""
        path = self._cache_dir / f"{team_id}.png"
        if path.is_file():
            cached = _decode(path.read_bytes())
            if cached is not None:
                return cached

        url = _LOGO_URL.format(team_id=team_id, size=self._size)
        data = self._fetch(url)
        if data is None:
            return None
        image = _decode(data)
        if image is None:
            return None
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return image

    def _fetch(self, url: str) -> bytes | None:
        try:
            with self._client.stream("GET", url, timeout=_TIMEOUT) as response:
                if response.status_code != 200:
                    return None
                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > _MAX_LOGO_BYTES:
                        logger.warning("logo %s exceeded the size cap", url)
                        return None
                    chunks.append(chunk)
                return b"".join(chunks)
        except httpx.HTTPError as error:
            logger.warning("logo fetch failed for %s: %s", url, error)
            return None


def _team(info: dict, line: dict) -> Team:
    return Team(
        id=info.get("id"),
        abbrev=info.get("abbreviation", ""),
        runs=line.get("runs") or 0,
    )


def _half_runs(feed: dict, inning: int, side: str) -> int:
    innings = feed.get("liveData", {}).get("linescore", {}).get("innings", [])
    if 1 <= inning <= len(innings):
        return innings[inning - 1].get(side, {}).get("runs") or 0
    return 0


def _current_half_scoring_play(feed: dict, game: GameState) -> str:
    plays = feed.get("liveData", {}).get("plays", {})
    scoring = plays.get("scoringPlays", [])
    all_plays = plays.get("allPlays", [])
    if not scoring:
        return ""
    index = scoring[-1]
    if not 0 <= index < len(all_plays):
        return ""
    about = all_plays[index].get("about", {})
    half = "top" if game.inning_state == "Top" else "bottom"
    if about.get("inning") == game.inning and about.get("halfInning") == half:
        return all_plays[index].get("result", {}).get("description") or ""
    return ""


def _ended_at(feed: dict) -> datetime | None:
    plays = feed.get("liveData", {}).get("plays", {}).get("allPlays", [])
    stamp = plays[-1].get("about", {}).get("endTime") if plays else None
    if not stamp:
        stamp = feed.get("gameData", {}).get("datetime", {}).get("dateTime")
    return _parse_dt(stamp)


def _parse_dt(stamp: str | None) -> datetime | None:
    if not stamp:
        return None
    try:
        return datetime.fromisoformat(stamp)
    except ValueError:
        return None


def _abstract_state(game: dict) -> str | None:
    return game.get("status", {}).get("abstractGameState")


def _unplayable(game: dict) -> bool:
    """Whether a game is postponed or suspended, so there is nothing to follow."""
    detailed = game.get("status", {}).get("detailedState", "")
    return "Postponed" in detailed or "Suspended" in detailed


def _latest(games: list[dict]) -> int | None:
    return max(games, key=lambda g: g.get("gameDate", "")).get("gamePk")


def _decode(data: bytes | None) -> Image.Image | None:
    """Load ``data`` as an RGBA image, or ``None`` if it is not an image.

    The logos are palette PNGs with transparency expressed in bytes. Converting
    to RGBA on load settles that transparency now, so a later conversion stays
    pristine instead of emitting Pillow's "should be converted to RGBA" warning.
    """
    if not data:
        return None
    try:
        with Image.open(io.BytesIO(data)) as image:
            return image.convert("RGBA")
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError):
        return None
