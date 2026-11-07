from __future__ import annotations

import json
import logging
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import httpx
from PIL import Image

from nightstand.app import AppState, LiveSources, run_forever, run_once
from nightstand.config import Config, Location, Schedule
from nightstand.models import ArtPiece, Health, NowPlaying
from nightstand.palette import PURE
from nightstand.screens.common import HEIGHT, WIDTH
from nightstand.sources import mlb
from nightstand.sources import weather as weather_source

FIXTURES = Path(__file__).parent / "fixtures"
# A Friday, at noon: outside the quiet window and the morning window.
NOON = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
# Inside the default weekday morning window (06:00 to 08:30).
MORNING = datetime(2026, 11, 6, 7, 0, tzinfo=UTC)


def _loop_config(**schedule_overrides) -> Config:
    # Quiet window [00:00, 00:00) never matches, so pushes are driven purely by
    # the redraw key unless a test asks for quiet hours. A fixed timezone keeps
    # the wall-clock windows deterministic regardless of the host zone.
    defaults = {"quiet_start": "00:00", "quiet_end": "00:00"}
    schedule = replace(Schedule(), **{**defaults, **schedule_overrides})
    return Config(schedule=schedule, location=Location(timezone="UTC"))


class FakeSources:
    """A hand-set ``Sources`` for driving the loop without any I/O."""

    def __init__(self) -> None:
        self.np: NowPlaying | None = None
        self.cover_img: Image.Image | None = None
        self.game_state: mlb.GameState | None = None
        self.logo_img: Image.Image | None = None
        self.forecast_obj = None
        self.art_piece: ArtPiece | None = None
        self.health_obj = Health()

    def now_playing(self):
        return self.np

    def cover(self, np):
        return self.cover_img

    def game(self):
        return self.game_state

    def logo(self, team_id):
        return self.logo_img

    def forecast(self):
        return self.forecast_obj

    def art(self, now):
        return self.art_piece

    def health(self):
        return self.health_obj


class RecordingDisplay:
    def __init__(self) -> None:
        self.shown: list[Image.Image] = []

    def show(self, img: Image.Image) -> None:
        self.shown.append(img)


def _marker_present(img: Image.Image) -> bool:
    return img.convert("RGB").getpixel((WIDTH - 8 - 7, 8 + 7)) == PURE["red"]


def test_unchanged_world_pushes_once_across_three_cycles():
    cfg = _loop_config()
    sources = FakeSources()
    sources.np = NowPlaying("Song", "Artist")
    display = RecordingDisplay()
    state = AppState()

    results = [run_once(state, cfg, sources, display, NOON) for _ in range(3)]

    assert results == [True, False, False]
    assert len(display.shown) == 1
    assert display.shown[0].size == (WIDTH, HEIGHT)


def test_now_playing_change_pushes():
    cfg = _loop_config()
    sources = FakeSources()
    display = RecordingDisplay()
    state = AppState()

    sources.np = NowPlaying("First", "A")
    assert run_once(state, cfg, sources, display, NOON) is True
    sources.np = NowPlaying("Second", "B")
    assert run_once(state, cfg, sources, display, NOON) is True

    assert len(display.shown) == 2


def test_health_problem_adds_marker_and_pushes_once():
    cfg = _loop_config()
    sources = FakeSources()
    sources.np = NowPlaying("Song", "Artist")
    display = RecordingDisplay()
    state = AppState()

    assert run_once(state, cfg, sources, display, NOON) is True
    assert not _marker_present(display.shown[-1])

    sources.health_obj = Health(problems=("pi-office.local behind main",))
    assert run_once(state, cfg, sources, display, NOON) is True
    assert _marker_present(display.shown[-1])
    # No further change: the marker state is part of the redraw key.
    assert run_once(state, cfg, sources, display, NOON) is False
    assert len(display.shown) == 2


def test_recovery_clears_marker_and_pushes_once():
    cfg = _loop_config()
    sources = FakeSources()
    sources.np = NowPlaying("Song", "Artist")
    sources.health_obj = Health(stale_since=datetime(2026, 11, 6, 11, 0, tzinfo=UTC))
    display = RecordingDisplay()
    state = AppState()

    assert run_once(state, cfg, sources, display, NOON) is True
    assert _marker_present(display.shown[-1])

    sources.health_obj = Health()
    assert run_once(state, cfg, sources, display, NOON) is True
    assert not _marker_present(display.shown[-1])


def test_quiet_hours_suppress_non_game_push():
    cfg = _loop_config(quiet_start="00:00", quiet_end="23:59")
    sources = FakeSources()
    sources.np = NowPlaying("Song", "Artist")
    display = RecordingDisplay()
    state = AppState()

    assert run_once(state, cfg, sources, display, NOON) is False
    assert display.shown == []


def test_run_once_renders_game_with_logos():
    cfg = _loop_config()
    feed = json.loads((FIXTURES / "mlb" / "feed_scoring_play.json").read_text())
    sources = FakeSources()
    sources.game_state = mlb.parse_game(feed)
    assert sources.game_state is not None and sources.game_state.status == "live"
    sources.logo_img = Image.new("RGB", (150, 150), PURE["blue"])
    display = RecordingDisplay()

    assert run_once(AppState(), cfg, sources, display, NOON) is True
    assert display.shown[0].size == (WIDTH, HEIGHT)


def test_run_once_renders_art_from_piece(tmp_path):
    cfg = _loop_config()
    art_path = tmp_path / "piece.png"
    Image.new("RGB", (400, 300), PURE["green"]).save(art_path)
    sources = FakeSources()
    sources.art_piece = ArtPiece(path=art_path, title="Title", artist="Artist")
    display = RecordingDisplay()

    assert run_once(AppState(), cfg, sources, display, NOON) is True
    assert display.shown[0].size == (WIDTH, HEIGHT)


def test_run_once_renders_weather_in_morning_window():
    cfg = _loop_config()
    data = json.loads((FIXTURES / "openmeteo" / "forecast.json").read_text())
    sources = FakeSources()
    sources.forecast_obj = weather_source.parse_forecast(data, MORNING)
    assert sources.forecast_obj is not None
    display = RecordingDisplay()

    assert run_once(AppState(), cfg, sources, display, MORNING) is True
    assert display.shown[0].size == (WIDTH, HEIGHT)


def test_run_once_renders_placeholder_when_nothing_matches():
    cfg = _loop_config()
    sources = FakeSources()
    display = RecordingDisplay()

    assert run_once(AppState(), cfg, sources, display, NOON) is True
    assert display.shown[0].size == (WIDTH, HEIGHT)


def test_run_forever_runs_each_cycle_and_sleeps():
    cfg = _loop_config()
    sources = FakeSources()
    sources.np = NowPlaying("Song", "Artist")
    display = RecordingDisplay()
    sleeps: list[int] = []

    class _Stop(Exception):
        pass

    def fake_sleep(seconds: int) -> None:
        sleeps.append(seconds)
        if len(sleeps) >= 3:
            raise _Stop

    try:
        run_forever(
            cfg,
            sources=sources,
            display=display,
            sleep=fake_sleep,
            clock=lambda: NOON,
        )
    except _Stop:
        pass

    assert sleeps == [cfg.schedule.check_seconds] * 3
    # The world never changes, so three cycles still push only once.
    assert len(display.shown) == 1


# --- LiveSources: the real source wiring, with a fake transport. ------------


class _Response:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class _SnapserverClient:
    """A fake client that fails or serves a recorded Server.GetStatus."""

    def __init__(self, status: dict) -> None:
        self.fail = True
        self._status = status

    def post(self, url, json=None, timeout=None):
        if self.fail:
            raise httpx.ConnectError("snapserver unreachable")
        return _Response(self._status)

    def get(self, url, params=None, timeout=None):
        # Health polling and anything else: unreachable but within the grace.
        raise httpx.ConnectError("unreachable")


def test_live_sources_outage_logs_once_and_sets_stale(tmp_path, caplog):
    cfg = Config()
    status = json.loads(
        (FIXTURES / "snapserver" / "playing_airplay.json").read_text()
    )
    client = _SnapserverClient(status)
    client.fail = True
    sources = LiveSources(cfg, client, cache_dir=tmp_path, clock=lambda: NOON)

    with caplog.at_level(logging.WARNING, logger="nightstand.app"):
        for _ in range(5):
            assert sources.now_playing() is None

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1

    health = sources.health()
    assert health.stale_since == NOON
    # The health URLs are unreachable but only just now, so within the grace.
    assert health.problems == ()


def test_live_sources_recovery_clears_stale(tmp_path):
    cfg = Config()
    status = json.loads(
        (FIXTURES / "snapserver" / "playing_airplay.json").read_text()
    )
    client = _SnapserverClient(status)
    sources = LiveSources(cfg, client, cache_dir=tmp_path, clock=lambda: NOON)

    client.fail = True
    assert sources.now_playing() is None
    assert sources.health().stale_since == NOON

    client.fail = False
    np = sources.now_playing()
    assert np is not None and np.title
    assert sources.health().stale_since is None


def test_live_sources_forecast_disabled_without_coordinates(tmp_path):
    # Default config leaves coordinates unset, so the forecast is off and no
    # request is attempted (a request would raise on this fake client).
    cfg = Config()
    sources = LiveSources(cfg, _SnapserverClient({}), cache_dir=tmp_path)

    assert sources.forecast() is None
