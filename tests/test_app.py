from __future__ import annotations

import io
import json
import logging
import stat
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
from PIL import Image

from nightstand import rules
from nightstand.app import (
    AppState,
    ArtView,
    LiveSources,
    feed_due,
    forecast_due,
    run_forever,
    run_once,
    schedule_due,
)
from nightstand.config import Art, Config, Location, Schedule
from nightstand.history import ArtHistory
from nightstand.models import ArtPiece, Health, NowPlaying
from nightstand.palette import PURE
from nightstand.screens.common import HEIGHT, WIDTH
from nightstand.sources import mlb
from nightstand.sources import weather as weather_source
from nightstand.sources.framecontrol import (
    FrameControl,
    FrameSettings,
    MorningWeather,
    Pin,
    WeatherLocation,
)

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


_UNSET = object()


def _frame_control(*, kinds=("music",), quiet=None, morning=None, pin=None):
    settings = FrameSettings(
        now_playing_kinds=kinds, quiet_hours=quiet, morning_weather=morning
    )
    return FrameControl(settings=settings, pin=pin)


class FakeSources:
    """A hand-set ``Sources`` for driving the loop without any I/O."""

    def __init__(self) -> None:
        self.np: NowPlaying | None = None
        self.cover_img: Image.Image | None = None
        self.game_state: mlb.GameState | None = None
        self.logo_img: Image.Image | None = None
        self.forecast_obj = None
        self.art_piece: ArtPiece | None = None
        self.art_forced = False
        self.health_obj = Health()
        self.control: FrameControl | None = None
        # Records the last pin hints the loop passed down, so a test can assert
        # the pin reached the source calls.
        self.forecast_pinned_weather = None
        self.forecast_weather_location = _UNSET
        self.art_pinned_piece = _UNSET

    def now_playing(self):
        return self.np

    def cover(self, np):
        return self.cover_img

    def game(self):
        return self.game_state

    def logo(self, team_id):
        return self.logo_img

    def frame_control(self):
        return self.control

    def forecast(
        self, now=None, cfg=None, *, weather_location=None, pinned_weather=False
    ):
        self.forecast_pinned_weather = pinned_weather
        self.forecast_weather_location = weather_location
        return self.forecast_obj

    def art(self, now, pinned_piece=None):
        self.art_pinned_piece = pinned_piece
        return ArtView(piece=self.art_piece, forced=self.art_forced)

    def commit_art(self, now):
        # A fixed-piece fake has no rotation to advance; the real wiring is
        # exercised by _RotatingArtSource below.
        pass

    def health(self):
        return self.health_obj


class RecordingDisplay:
    def __init__(self) -> None:
        self.shown: list[Image.Image] = []

    def show(self, img: Image.Image) -> None:
        self.shown.append(img)


def _marker_present(img: Image.Image) -> bool:
    return img.convert("RGB").getpixel((WIDTH - 8 - 7, 8 + 7)) == PURE["red"]


def _png_bytes(color: tuple[int, int, int] = (10, 20, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(buf, format="PNG")
    return buf.getvalue()


def _now_playing_doc() -> dict:
    return json.loads(
        (FIXTURES / "nowplaying" / "spec_example.json").read_text()
    )


def _long_form(position: float, position_at: datetime) -> NowPlaying:
    return NowPlaying(
        title="Episode 412",
        artist="Show",
        album=None,
        rooms=("Office",),
        kind="long_form",
        status="playing",
        duration=7200.0,
        position=position,
        position_at=position_at,
    )


def test_unchanged_world_pushes_once_across_three_cycles():
    cfg = _loop_config()
    sources = FakeSources()
    sources.np = NowPlaying("Song", "Artist")
    display = RecordingDisplay()
    state = AppState()

    # The track settles after 20 s, then the unchanged world holds the panel.
    results = [
        run_once(state, cfg, sources, display, NOON + timedelta(seconds=offset))
        for offset in (0, 20, 40)
    ]

    assert results == [False, True, False]
    assert len(display.shown) == 1
    assert display.shown[0].size == (WIDTH, HEIGHT)


def test_now_playing_change_pushes():
    cfg = _loop_config()
    sources = FakeSources()
    display = RecordingDisplay()
    state = AppState()

    sources.np = NowPlaying("First", "A")
    assert run_once(state, cfg, sources, display, NOON) is False
    assert run_once(state, cfg, sources, display, NOON + timedelta(seconds=20)) is True
    sources.np = NowPlaying("Second", "B")
    settled = NOON + timedelta(seconds=20)
    assert run_once(state, cfg, sources, display, settled) is False
    resettled = settled + timedelta(seconds=20)
    assert run_once(state, cfg, sources, display, resettled) is True

    assert len(display.shown) == 2


def test_new_track_holds_until_settle_seconds():
    cfg = _loop_config()
    sources = FakeSources()
    sources.np = NowPlaying("Song", "Artist")
    display = RecordingDisplay()
    state = AppState()

    assert run_once(state, cfg, sources, display, NOON) is False
    assert (
        run_once(state, cfg, sources, display, NOON + timedelta(seconds=19)) is False
    )
    assert (
        run_once(state, cfg, sources, display, NOON + timedelta(seconds=20)) is True
    )
    assert len(display.shown) == 1


def test_skipping_three_tracks_in_thirty_seconds_pushes_once_for_the_last():
    cfg = _loop_config()
    sources = FakeSources()
    display = RecordingDisplay()
    state = AppState()

    sources.np = NowPlaying("One", "A")
    assert run_once(state, cfg, sources, display, NOON) is False
    sources.np = NowPlaying("Two", "B")
    assert (
        run_once(state, cfg, sources, display, NOON + timedelta(seconds=15)) is False
    )
    sources.np = NowPlaying("Three", "C")
    assert (
        run_once(state, cfg, sources, display, NOON + timedelta(seconds=30)) is False
    )
    # "Three" appears at t=30 and must stay stable 20 s, so it pushes at t=50.
    assert (
        run_once(state, cfg, sources, display, NOON + timedelta(seconds=49)) is False
    )
    assert (
        run_once(state, cfg, sources, display, NOON + timedelta(seconds=50)) is True
    )
    assert len(display.shown) == 1


def test_stop_to_nothing_playing_waits_before_art(tmp_path):
    cfg = _loop_config()
    art_path = tmp_path / "piece.png"
    Image.new("RGB", (400, 300), PURE["green"]).save(art_path)
    sources = FakeSources()
    sources.np = NowPlaying("Song", "Artist")
    sources.art_piece = ArtPiece(path=art_path, title="Title", artist=None)
    display = RecordingDisplay()
    state = AppState()

    # Settle and show the track.
    assert run_once(state, cfg, sources, display, NOON) is False
    assert run_once(state, cfg, sources, display, NOON + timedelta(seconds=20)) is True

    # Music stops. Art is the decision, but the stop must settle for 20 s first.
    sources.np = None
    stop = NOON + timedelta(seconds=60)
    assert run_once(state, cfg, sources, display, stop) is False
    assert run_once(state, cfg, sources, display, stop + timedelta(seconds=19)) is False
    assert run_once(state, cfg, sources, display, stop + timedelta(seconds=20)) is True
    assert len(display.shown) == 2


def test_bucket_change_for_the_same_track_pushes_immediately():
    cfg = _loop_config()
    sources = FakeSources()
    sources.control = _frame_control(kinds=("music", "podcast"))
    anchor = NOON
    sources.np = _long_form(0.0, anchor)
    display = RecordingDisplay()
    state = AppState()

    # Settle and show bucket 0.
    assert run_once(state, cfg, sources, display, NOON) is False
    assert run_once(state, cfg, sources, display, NOON + timedelta(seconds=20)) is True

    # Ten minutes in, the same track crosses into bucket 1 and pushes at once,
    # with no fresh settle wait.
    crossed = anchor + timedelta(seconds=600)
    assert run_once(state, cfg, sources, display, crossed) is True
    assert len(display.shown) == 2


def test_quiet_hours_hold_settled_change_then_first_push_carries_current_bucket():
    # Quiet hours [06:00, 12:30); NOON (12:00) is inside, 12:30 is the first
    # minute outside.
    cfg = _loop_config(quiet_start="06:00", quiet_end="12:30")
    sources = FakeSources()
    sources.control = _frame_control(kinds=("music", "podcast"))
    anchor = NOON
    sources.np = _long_form(0.0, anchor)
    display = RecordingDisplay()
    state = AppState()

    # The new track settles during quiet hours but quiet hours hold the push.
    assert run_once(state, cfg, sources, display, NOON) is False
    assert run_once(state, cfg, sources, display, NOON + timedelta(seconds=20)) is False
    assert run_once(state, cfg, sources, display, NOON + timedelta(minutes=10)) is False
    assert display.shown == []

    # Quiet hours end at 12:30. The track is now ~30 min in (bucket 3), and the
    # first push after quiet hours carries that current bucket.
    end = datetime(2026, 11, 6, 12, 30, tzinfo=UTC)
    assert run_once(state, cfg, sources, display, end) is True
    assert state.last_key[5] == 3
    assert len(display.shown) == 1


def test_health_problem_adds_marker_and_pushes_once():
    cfg = _loop_config()
    sources = FakeSources()
    sources.np = NowPlaying("Song", "Artist")
    display = RecordingDisplay()
    state = AppState()

    # Settle the track first; the marker change then pushes without a new wait.
    assert run_once(state, cfg, sources, display, NOON) is False
    settled = NOON + timedelta(seconds=20)
    assert run_once(state, cfg, sources, display, settled) is True
    assert not _marker_present(display.shown[-1])

    sources.health_obj = Health(problems=("office.local behind main",))
    assert run_once(state, cfg, sources, display, settled) is True
    assert _marker_present(display.shown[-1])
    # No further change: the marker state is part of the redraw key.
    assert run_once(state, cfg, sources, display, settled) is False
    assert len(display.shown) == 2


def test_recovery_clears_marker_and_pushes_once():
    cfg = _loop_config()
    sources = FakeSources()
    sources.np = NowPlaying("Song", "Artist")
    sources.health_obj = Health(problems=("office.local behind main",))
    display = RecordingDisplay()
    state = AppState()

    assert run_once(state, cfg, sources, display, NOON) is False
    settled = NOON + timedelta(seconds=20)
    assert run_once(state, cfg, sources, display, settled) is True
    assert _marker_present(display.shown[-1])

    sources.health_obj = Health()
    assert run_once(state, cfg, sources, display, settled) is True
    assert not _marker_present(display.shown[-1])


def test_data_source_outage_draws_no_marker(tmp_path):
    # A stale data source (an outage within the health grace) is not a machine
    # health problem, so it must not draw the corner marker.
    cfg = Config(location=Location(timezone="UTC"))
    client = _HubClient(_now_playing_doc())
    client.fail = True
    live = LiveSources(cfg, client, cache_dir=tmp_path, clock=lambda: NOON)
    display = RecordingDisplay()

    # The now-playing read fails, degrading to nothing playing (identity None),
    # so the placeholder pushes at once without a settle wait.
    assert run_once(AppState(), cfg, live, display, NOON) is True
    assert live.health().problems == ()
    assert not _marker_present(display.shown[-1])


def test_quiet_hours_suppress_non_game_push():
    cfg = _loop_config(quiet_start="00:00", quiet_end="23:59")
    sources = FakeSources()
    sources.np = NowPlaying("Song", "Artist")
    display = RecordingDisplay()
    # The track is already the shown identity, so the settle does not mask the
    # quiet-hours suppression we are checking.
    state = AppState(shown_identity=("Song", "Artist", None))

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


def test_run_once_records_art_history_when_art_pushed(tmp_path):
    cfg = _loop_config()
    art_path = tmp_path / "piece.png"
    Image.new("RGB", (400, 300), PURE["green"]).save(art_path)
    sources = FakeSources()
    sources.art_piece = ArtPiece(
        path=art_path,
        title="Title",
        artist="Artist",
        source_url="https://x.org/1",
        id="Photos/piece.png",
    )
    history = ArtHistory(tmp_path / "art-history.json")

    assert run_once(AppState(), cfg, sources, RecordingDisplay(), NOON, history) is True

    data = json.loads((tmp_path / "art-history.json").read_text())
    assert data["current"]["file"] == "piece.png"
    assert data["current"]["id"] == "Photos/piece.png"
    assert data["current"]["source_url"] == "https://x.org/1"


def test_run_once_non_art_screen_does_not_record_history(tmp_path):
    cfg = _loop_config()
    sources = FakeSources()
    sources.np = NowPlaying("Song", "Artist")
    history = ArtHistory(tmp_path / "art-history.json")
    state = AppState()
    display = RecordingDisplay()

    # Settle, then push the now-playing screen: a non-art push writes no history.
    assert run_once(state, cfg, sources, display, NOON, history) is False
    settled = NOON + timedelta(seconds=20)
    assert run_once(state, cfg, sources, display, settled, history) is True

    assert not (tmp_path / "art-history.json").exists()


def test_art_history_records_once_across_repeated_cycles(tmp_path):
    cfg = _loop_config()
    art_path = tmp_path / "piece.png"
    Image.new("RGB", (400, 300), PURE["green"]).save(art_path)
    sources = FakeSources()
    sources.art_piece = ArtPiece(path=art_path, title="Title", artist=None)
    history = ArtHistory(tmp_path / "art-history.json")
    state = AppState()
    display = RecordingDisplay()

    for _ in range(3):
        run_once(state, cfg, sources, display, NOON, history)

    data = json.loads((tmp_path / "art-history.json").read_text())
    assert [entry["file"] for entry in data["history"]] == ["piece.png"]


class _RaisingHistory:
    """A history double whose record always raises, to test the loop survives."""

    def __init__(self, error: Exception) -> None:
        self._error = error
        self.calls = 0

    def record(self, piece, shown_at) -> bool:
        self.calls += 1
        raise self._error


def test_run_once_survives_history_write_error(caplog):
    # A write failure (e.g. public/ not yet writable) must not stop the loop or
    # the display push: run_once still returns normally and the panel is drawn.
    cfg = _loop_config()
    sources = FakeSources()
    sources.art_piece = ArtPiece(path=Path("piece.png"), title="Title", artist=None)
    display = RecordingDisplay()

    for error in (PermissionError("read-only"), OSError("disk full")):
        history = _RaisingHistory(error)
        state = AppState()
        with caplog.at_level(logging.WARNING, logger="nightstand.app"):
            result = run_once(state, cfg, sources, display, NOON, history)
        assert result is True
        assert history.calls == 1
        assert len(display.shown) >= 1
        assert display.shown[-1].size == (WIDTH, HEIGHT)


def test_run_once_history_write_error_logged_once_per_outage(caplog):
    # The failure is logged on the state change into failure, not every cycle.
    cfg = _loop_config()
    sources = FakeSources()
    sources.art_piece = ArtPiece(path=Path("a.png"), title="A", artist=None)
    display = RecordingDisplay()
    history = _RaisingHistory(PermissionError("read-only"))
    state = AppState()

    with caplog.at_level(logging.WARNING, logger="nightstand.app"):
        for _ in range(3):
            # A fresh piece each cycle so the art decision keeps trying to record.
            sources.art_piece = replace(sources.art_piece, path=Path(f"{_}.png"))
            run_once(state, cfg, sources, display, NOON, history)

    warnings = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING
        and r.name == "nightstand.app"
        and "art history write failed" in r.message
    ]
    assert len(warnings) == 1


def test_forced_art_command_pushes_in_quiet_hours(tmp_path):
    # Someone pressed Next on the page: the redraw bypasses quiet hours.
    cfg = _loop_config(quiet_start="00:00", quiet_end="23:59")
    art_path = tmp_path / "piece.png"
    Image.new("RGB", (400, 300), PURE["green"]).save(art_path)
    sources = FakeSources()
    sources.art_piece = ArtPiece(path=art_path, title="Title", id="piece.png")
    sources.art_forced = True
    display = RecordingDisplay()

    assert run_once(AppState(), cfg, sources, display, NOON) is True
    assert len(display.shown) == 1


def test_scheduled_art_is_held_in_quiet_hours(tmp_path):
    # No command: the scheduled rotation keeps the quiet-hours hold.
    cfg = _loop_config(quiet_start="00:00", quiet_end="23:59")
    art_path = tmp_path / "piece.png"
    Image.new("RGB", (400, 300), PURE["green"]).save(art_path)
    sources = FakeSources()
    sources.art_piece = ArtPiece(path=art_path, title="Title", id="piece.png")
    sources.art_forced = False
    display = RecordingDisplay()

    assert run_once(AppState(), cfg, sources, display, NOON) is False
    assert display.shown == []


class _RotatingArtSource(FakeSources):
    """A fake backed by a real ``Rotation``, mirroring what ``LiveSources`` does.

    ``art`` decides the piece (``Rotation.current``) and ``commit_art`` confirms
    it was shown (``Rotation.commit``), so the loop advances the rotation only
    when the art screen actually reaches the panel (Important-1).
    """

    def __init__(self, rotation, eligible, interval: int = 60) -> None:
        super().__init__()
        self._rotation = rotation
        self._eligible = eligible
        self._interval = interval

    def art(self, now, pinned_piece=None):
        piece = self._rotation.current(self._eligible, now, self._interval)
        return ArtView(piece=piece, forced=False)

    def commit_art(self, now):
        self._rotation.commit(now)


def _art_pieces(tmp_path, n: int):
    pieces = []
    for i in range(n):
        path = tmp_path / f"p{i}.png"
        Image.new("RGB", (400, 300), PURE["green"]).save(path)
        pieces.append(ArtPiece(path=path, title=f"P{i}", id=f"p{i}.png"))
    return pieces


def _rotation_state(tmp_path) -> dict:
    return json.loads((tmp_path / "rotation.json").read_text())


def test_scheduled_art_does_not_advance_while_music_holds_the_screen(tmp_path):
    # Important-1: music is on the panel across two intervals, so the rotation
    # must not step and the back stack must stay empty.
    from nightstand.rotation import Rotation

    cfg = _loop_config()  # quiet window never matches
    elig = _art_pieces(tmp_path, 5)
    clock = _Clock(NOON)
    rot = Rotation(tmp_path / "rotation.json", clock=clock)
    sources = _RotatingArtSource(rot, elig)
    display = RecordingDisplay()
    state = AppState()

    # Art reaches the panel first; the rotation commits its first piece.
    assert run_once(state, cfg, sources, display, clock()) is True
    first_id = _rotation_state(tmp_path)["current_id"]
    assert first_id is not None

    # Music starts and holds the screen across two full intervals.
    sources.np = NowPlaying("Song", "Artist")
    clock.advance(20)
    run_once(state, cfg, sources, display, clock())  # music settles and pushes
    clock.now = NOON + timedelta(minutes=60)
    run_once(state, cfg, sources, display, clock())
    clock.now = NOON + timedelta(minutes=120)
    run_once(state, cfg, sources, display, clock())

    after = _rotation_state(tmp_path)
    assert after["current_id"] == first_id  # no advance while music held
    assert after["back"] == []


def test_scheduled_art_does_not_advance_during_quiet_hours(tmp_path):
    from nightstand.rotation import Rotation

    # Quiet hours [13:00, 14:00); NOON (12:00) is outside, 13:30 is inside.
    cfg = _loop_config(quiet_start="13:00", quiet_end="14:00")
    elig = _art_pieces(tmp_path, 5)
    clock = _Clock(NOON)
    rot = Rotation(tmp_path / "rotation.json", clock=clock)
    sources = _RotatingArtSource(rot, elig)
    display = RecordingDisplay()
    state = AppState()

    # Art shown first, outside quiet hours; the rotation commits a piece.
    assert run_once(state, cfg, sources, display, clock()) is True
    first_id = _rotation_state(tmp_path)["current_id"]

    # An interval later, now inside quiet hours: the push is held, so no advance.
    clock.now = datetime(2026, 11, 6, 13, 30, tzinfo=UTC)
    assert run_once(state, cfg, sources, display, clock()) is False
    assert display.shown == [display.shown[0]]  # only the first push

    after = _rotation_state(tmp_path)
    assert after["current_id"] == first_id
    assert after["back"] == []


def test_art_advances_once_when_it_returns_after_a_long_gap(tmp_path):
    from nightstand.rotation import Rotation

    cfg = _loop_config()  # quiet window never matches
    elig = _art_pieces(tmp_path, 5)
    clock = _Clock(NOON)
    rot = Rotation(tmp_path / "rotation.json", clock=clock)
    sources = _RotatingArtSource(rot, elig)
    display = RecordingDisplay()
    state = AppState()

    # Art shown first; the rotation commits piece A.
    assert run_once(state, cfg, sources, display, clock()) is True
    a_id = _rotation_state(tmp_path)["current_id"]

    # Music holds the screen for five hours (many missed intervals).
    sources.np = NowPlaying("Song", "Artist")
    clock.advance(20)
    run_once(state, cfg, sources, display, clock())  # music pushes
    clock.now = NOON + timedelta(hours=5)
    run_once(state, cfg, sources, display, clock())  # still music

    # Music stops; art returns and settles.
    sources.np = None
    stop = NOON + timedelta(hours=5, seconds=30)
    clock.now = stop
    assert run_once(state, cfg, sources, display, clock()) is False  # settle wait
    clock.now = stop + timedelta(seconds=20)
    assert run_once(state, cfg, sources, display, clock()) is True  # art pushes

    after = _rotation_state(tmp_path)
    b_id = after["current_id"]
    assert b_id != a_id  # advanced exactly one step
    assert after["back"] == [a_id]  # A, a piece actually drawn, is behind us

    # Back walks to A. The first command of the process is adopted as a baseline
    # (startup grace), so the second one acts.
    now = clock()
    assert rot.apply(_control_cmd(1, "previous"), elig, now) is False
    assert rot.apply(_control_cmd(2, "previous"), elig, now) is True
    assert rot.current(elig, now, 60).id == a_id


def _control_cmd(seq: int, action: str):
    from nightstand.sources.artcontrol import ArtControl

    return ArtControl(
        rotate_minutes=60, folders_enabled=(), seq=seq, action=action, piece=None
    )


def test_live_sources_art_follows_hub_control_and_commands(tmp_path):
    art_dir = tmp_path / "art"
    art_dir.mkdir()
    for name in ("a.png", "b.png"):
        Image.new("RGB", (300, 200), PURE["green"]).save(art_dir / name)
    control = {
        "settings": {"rotate_minutes": 60, "folders_enabled": ["Collection"]},
        "command": {"seq": 5, "action": "next", "piece": None},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/art-control.json":
            return httpx.Response(200, json=control)
        raise AssertionError(f"unexpected request {request.url}")

    cfg = Config(art=Art(dir=art_dir), location=Location(timezone="UTC"))
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        sources = LiveSources(
            cfg, client, cache_dir=tmp_path, state_dir=tmp_path / "state",
            clock=lambda: NOON,
        )
        view = sources.art(NOON)
        assert view.piece is not None
        assert view.forced is False  # first command seen at startup is adopted
        # The frame drew that piece, so the rotation commits it; a later "next"
        # then advances off it.
        sources.commit_art(NOON)

        control["command"]["seq"] = 6
        forced_view = sources.art(NOON)
        assert forced_view.forced is True
        assert forced_view.piece is not None
        assert forced_view.piece.id != view.piece.id


def test_live_sources_art_falls_back_to_local_config_without_hub_control(tmp_path):
    art_dir = tmp_path / "art"
    art_dir.mkdir()
    Image.new("RGB", (300, 200), PURE["green"]).save(art_dir / "only.png")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    cfg = Config(art=Art(dir=art_dir), location=Location(timezone="UTC"))
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        sources = LiveSources(
            cfg, client, cache_dir=tmp_path, state_dir=tmp_path / "state",
            clock=lambda: NOON,
        )
        view = sources.art(NOON)

    assert view.piece is not None
    assert view.piece.id == "only.png"
    assert view.forced is False


def _control_min_aspect_handler(min_aspect):
    settings = {"rotate_minutes": 60, "folders_enabled": ["Collection"]}
    if min_aspect is not None:
        settings["min_aspect"] = min_aspect
    control = {"settings": settings, "command": None}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/art-control.json":
            return httpx.Response(200, json=control)
        raise AssertionError(f"unexpected request {request.url}")

    return handler


def test_live_sources_art_control_min_aspect_changes_eligibility(tmp_path):
    # One piece at aspect 1.2. The local cutoff (1.3) excludes it, but the hub's
    # control file lowers the cutoff to 1.1, so it becomes eligible and is shown.
    art_dir = tmp_path / "art"
    art_dir.mkdir()
    Image.new("RGB", (240, 200), PURE["green"]).save(art_dir / "mid.png")

    cfg = Config(art=Art(dir=art_dir), location=Location(timezone="UTC"))
    with httpx.Client(
        transport=httpx.MockTransport(_control_min_aspect_handler(1.1))
    ) as client:
        sources = LiveSources(
            cfg, client, cache_dir=tmp_path, state_dir=tmp_path / "state",
            clock=lambda: NOON,
        )
        view = sources.art(NOON)

    assert view.piece is not None
    assert view.piece.id == "mid.png"


def test_live_sources_art_min_aspect_falls_back_to_config(tmp_path):
    # The control file carries no min_aspect, so the local cutoff (1.5) governs
    # and the 1.2 piece is skipped: nothing eligible.
    art_dir = tmp_path / "art"
    art_dir.mkdir()
    Image.new("RGB", (240, 200), PURE["green"]).save(art_dir / "mid.png")

    cfg = Config(
        art=Art(dir=art_dir, min_aspect=1.5), location=Location(timezone="UTC")
    )
    with httpx.Client(
        transport=httpx.MockTransport(_control_min_aspect_handler(None))
    ) as client:
        sources = LiveSources(
            cfg, client, cache_dir=tmp_path, state_dir=tmp_path / "state",
            clock=lambda: NOON,
        )
        view = sources.art(NOON)

    assert view.piece is None


def _read_mirror(public: Path) -> dict:
    return json.loads((public / "frame.json").read_text())


def test_run_once_writes_frame_mirror_after_push(tmp_path):
    cfg = replace(_loop_config(), paths=replace(_loop_config().paths, public=tmp_path))
    art_path = tmp_path / "piece.png"
    Image.new("RGB", (400, 300), PURE["green"]).save(art_path)
    sources = FakeSources()
    sources.art_piece = ArtPiece(path=art_path, title="Title", artist="Artist")

    assert run_once(AppState(), cfg, sources, RecordingDisplay(), NOON) is True

    mirror = Image.open(tmp_path / "frame.png")
    assert mirror.size == (WIDTH, HEIGHT)
    meta = _read_mirror(tmp_path)
    assert meta == {"updated_at": NOON.isoformat(), "screen": "art"}
    assert stat.S_IMODE((tmp_path / "frame.png").stat().st_mode) == 0o644
    assert stat.S_IMODE((tmp_path / "frame.json").stat().st_mode) == 0o644


def test_run_once_mirror_not_written_on_a_held_cycle(tmp_path):
    cfg = replace(_loop_config(), paths=replace(_loop_config().paths, public=tmp_path))
    sources = FakeSources()
    sources.np = NowPlaying("Song", "Artist")
    state = AppState()
    display = RecordingDisplay()

    # A changed identity is held by the settle: nothing pushes, no mirror.
    assert run_once(state, cfg, sources, display, NOON) is False
    assert not (tmp_path / "frame.png").exists()
    assert not (tmp_path / "frame.json").exists()

    # Once settled it pushes and the mirror records the now-playing screen.
    settled = NOON + timedelta(seconds=20)
    assert run_once(state, cfg, sources, display, settled, None) is True
    assert _read_mirror(tmp_path)["screen"] == "now_playing"


def test_run_once_survives_mirror_write_error(tmp_path, caplog):
    # public/ is a plain file, so every mirror write raises. The push must still
    # succeed and the loop must not crash; the failure is logged once per outage.
    blocked = tmp_path / "public"
    blocked.write_text("not a directory")
    cfg = replace(_loop_config(), paths=replace(_loop_config().paths, public=blocked))
    art_path = tmp_path / "piece.png"
    Image.new("RGB", (400, 300), PURE["green"]).save(art_path)
    sources = FakeSources()
    state = AppState()
    display = RecordingDisplay()

    with caplog.at_level(logging.WARNING, logger="nightstand.app"):
        for i in range(3):
            sources.art_piece = ArtPiece(path=art_path, title="Title", id=f"{i}.png")
            assert run_once(state, cfg, sources, display, NOON) is True

    assert len(display.shown) == 3
    warnings = [
        r
        for r in caplog.records
        if r.levelname == "WARNING" and "frame mirror write failed" in r.message
    ]
    assert len(warnings) == 1


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
    # Nothing playing (identity None) so the placeholder pushes at once; this
    # test is about the loop and sleep cadence, not the settle.
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
        self.request = httpx.Request("POST", "http://hub.local/jsonrpc")

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload

    def iter_bytes(self):
        yield json.dumps(self._payload).encode("utf-8")

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class _HubClient:
    """A fake client that fails or serves the hub's now-playing.json over GET.

    Health polling and any other GET is unreachable but within the grace, so a
    now-playing outage does not leak into the health marker.
    """

    def __init__(self, doc: dict) -> None:
        self.fail = True
        self._doc = doc

    def get(self, url, params=None, timeout=None):
        if url.endswith("/now-playing.json"):
            if self.fail:
                raise httpx.ConnectError("hub unreachable")
            return _Response(self._doc)
        raise httpx.ConnectError("unreachable")


def test_live_sources_outage_logs_once(tmp_path, caplog):
    cfg = Config()
    client = _HubClient(_now_playing_doc())
    client.fail = True
    sources = LiveSources(cfg, client, cache_dir=tmp_path, clock=lambda: NOON)

    with caplog.at_level(logging.WARNING, logger="nightstand.app"):
        for _ in range(5):
            assert sources.now_playing() is None

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1

    # A source outage is not a machine-health problem, so it draws no marker.
    # The health URLs are unreachable but only just now, so within the grace.
    assert sources.health().problems == ()


def test_live_sources_recovery_logs_once(tmp_path, caplog):
    cfg = Config()
    client = _HubClient(_now_playing_doc())
    sources = LiveSources(cfg, client, cache_dir=tmp_path, clock=lambda: NOON)

    client.fail = True
    assert sources.now_playing() is None

    client.fail = False
    with caplog.at_level(logging.INFO, logger="nightstand.app"):
        np = sources.now_playing()
    assert np is not None and np.title

    recovered = [
        r
        for r in caplog.records
        if r.levelno == logging.INFO and "recovered" in r.getMessage()
    ]
    assert len(recovered) == 1


def test_live_sources_now_playing_and_cover_from_hub(tmp_path):
    doc = _now_playing_doc()
    cover = _png_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/now-playing.json":
            return httpx.Response(200, json=doc)
        if request.url.path == "/cover/3f2a9b7c.jpg":
            return httpx.Response(200, content=cover)
        raise AssertionError(f"unexpected request {request.url}")

    cfg = Config(location=Location(timezone="UTC"))
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        sources = LiveSources(cfg, client, cache_dir=tmp_path, clock=lambda: NOON)
        np = sources.now_playing()
        assert np is not None
        assert np.title.startswith("Episode 412")
        assert np.kind == "long_form"
        assert np.rooms == ("Bedroom", "Office")
        image = sources.cover(np)
        assert isinstance(image, Image.Image)


def test_live_sources_malformed_now_playing_degrades_to_none(tmp_path):
    # Review Focus 3: a malformed now-playing.json means nothing playing, never
    # a crash or stale data.
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/now-playing.json":
            return httpx.Response(200, content=b"<html>broken</html>")
        raise httpx.ConnectError("unreachable")

    cfg = Config(location=Location(timezone="UTC"))
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        sources = LiveSources(cfg, client, cache_dir=tmp_path, clock=lambda: NOON)
        assert sources.now_playing() is None


def test_live_sources_missing_now_playing_file_degrades_to_none(tmp_path):
    # An old hub or a service that has not written the file yet (404) is also
    # nothing playing.
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/now-playing.json":
            return httpx.Response(404)
        raise httpx.ConnectError("unreachable")

    cfg = Config(location=Location(timezone="UTC"))
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        sources = LiveSources(cfg, client, cache_dir=tmp_path, clock=lambda: NOON)
        assert sources.now_playing() is None


def test_live_sources_empty_rooms_is_nothing_playing(tmp_path):
    # The hub still publishes a track whose rooms are all muted; the frame treats
    # an empty rooms list as nothing playing.
    doc = _now_playing_doc()
    doc["rooms"] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=doc)

    cfg = Config(location=Location(timezone="UTC"))
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        sources = LiveSources(cfg, client, cache_dir=tmp_path, clock=lambda: NOON)
        assert sources.now_playing() is None


def test_live_sources_forecast_disabled_without_coordinates(tmp_path):
    # Default config leaves coordinates unset, so the forecast is off and no
    # request is attempted (a request would raise on this fake client).
    cfg = Config()
    sources = LiveSources(cfg, _HubClient({}), cache_dir=tmp_path)

    assert sources.forecast() is None


class _ForecastClient:
    """Serves the Open-Meteo forecast and records each request's parameters."""

    def __init__(self) -> None:
        self.params: list[dict] = []
        forecast_path = FIXTURES / "openmeteo" / "forecast.json"
        self._payload = json.loads(forecast_path.read_text())

    def get(self, url, params=None, timeout=None):
        if url == _FORECAST_URL:
            self.params.append(params or {})
            return _Response(self._payload)
        raise AssertionError(f"unexpected GET {url}")


def _hub_location() -> WeatherLocation:
    # A well-known city centre (London), never the household's real location.
    return WeatherLocation(
        name="London, England, United Kingdom",
        latitude=51.51,
        longitude=-0.13,
        timezone="Europe/London",
    )


def _tokyo_location() -> WeatherLocation:
    # Another well-known city centre, in a different timezone from London.
    return WeatherLocation(
        name="Tokyo, Japan",
        latitude=35.68,
        longitude=139.76,
        timezone="Asia/Tokyo",
    )


def test_hub_weather_location_overrides_the_local_location(tmp_path):
    # The local config points at one place; the hub's weather_location points at
    # London. The loop passes the hub location to forecast() explicitly, and the
    # request must use the hub's coordinates and zone. The local zone is UTC so
    # MORNING falls in the morning window and a fetch actually happens.
    local = Config(
        location=Location(latitude=40.71, longitude=-74.01, timezone="UTC")
    )

    client = _ForecastClient()
    sources = LiveSources(local, client, cache_dir=tmp_path, clock=lambda: MORNING)
    forecast = sources.forecast(MORNING, local, weather_location=_hub_location())

    assert forecast is not None
    assert len(client.params) == 1
    assert client.params[0]["latitude"] == 51.51
    assert client.params[0]["longitude"] == -0.13
    assert client.params[0]["timezone"] == "Europe/London"


def test_hub_timezone_applies_to_the_forecast_not_the_schedule(tmp_path):
    # Ruling: the hub city's timezone moves only the forecast (its request and
    # displayed times), never the schedule clock. A frame in Pacific time with a
    # London weather city still evaluates quiet hours and the morning window on
    # Pacific time, so choosing a far-away city cannot wake the panel at night.
    local = Config(
        location=Location(
            latitude=32.72, longitude=-117.16, timezone="America/Los_Angeles"
        )
    )
    settings = FrameSettings(
        now_playing_kinds=("music",),
        quiet_hours=None,
        morning_weather=None,
        weather_location=_hub_location(),
    )
    effective = rules.effective_config(local, settings)

    # The schedule clock is untouched: quiet-hours and morning decisions match
    # the local config at every hour of the day.
    base = datetime(2026, 11, 6, 0, 0, tzinfo=UTC)
    for hour in range(24):
        now = base + timedelta(hours=hour)
        assert rules.in_quiet_hours(now, effective) == rules.in_quiet_hours(now, local)
        assert rules.in_morning_window(now, effective) == rules.in_morning_window(
            now, local
        )

    # The forecast request, however, uses the hub city's zone. pinned_weather
    # forces a fetch regardless of the Pacific morning window.
    client = _ForecastClient()
    sources = LiveSources(local, client, cache_dir=tmp_path, clock=lambda: MORNING)
    forecast = sources.forecast(
        MORNING, effective, weather_location=_hub_location(), pinned_weather=True
    )

    assert forecast is not None
    assert client.params[0]["timezone"] == "Europe/London"
    assert client.params[0]["latitude"] == 51.51


def test_changing_the_weather_location_refetches_within_the_ttl(tmp_path):
    # Review Important 2: when the hub changes the city, the next due forecast
    # must show the new place, not a cached one from the old city, even inside
    # the 30-minute TTL. The clock is fixed, so without cache invalidation the
    # second call would serve the stale cache and make no request.
    cfg = Config(location=Location(timezone="UTC"))  # coords come from the hub

    client = _ForecastClient()
    sources = LiveSources(cfg, client, cache_dir=tmp_path, clock=lambda: MORNING)

    first = sources.forecast(MORNING, cfg, weather_location=_hub_location())
    second = sources.forecast(MORNING, cfg, weather_location=_tokyo_location())

    assert first is not None and second is not None
    assert len(client.params) == 2
    assert client.params[0]["latitude"] == 51.51  # London
    assert client.params[1]["latitude"] == 35.68  # Tokyo
    assert client.params[1]["timezone"] == "Asia/Tokyo"


def test_invalid_hub_location_falls_back_to_the_local_location(tmp_path):
    # An invalid hub weather_location parses to None, so forecast() falls back to
    # the local [location]. A fixed UTC zone keeps the morning window deterministic.
    local = Config(
        location=Location(latitude=40.71, longitude=-74.01, timezone="UTC")
    )
    doc = {
        "settings": {
            "now_playing_kinds": ["music"],
            "weather_location": {
                "latitude": 999.0,  # out of range: the whole location is dropped
                "longitude": -0.13,
                "timezone": "Europe/London",
            },
        },
        "pin": None,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/frame-control.json":
            return httpx.Response(200, json=doc)
        raise AssertionError(f"unexpected request {request.url}")

    with httpx.Client(transport=httpx.MockTransport(handler)) as hub_client:
        control = sources_fetch_control(hub_client)
    assert control.settings.weather_location is None

    client = _ForecastClient()
    sources = LiveSources(local, client, cache_dir=tmp_path, clock=lambda: MORNING)
    forecast = sources.forecast(
        MORNING, local, weather_location=control.settings.weather_location
    )

    assert forecast is not None
    assert client.params[0]["latitude"] == 40.71
    assert client.params[0]["longitude"] == -74.01
    assert client.params[0]["timezone"] == "UTC"


def test_no_location_warning_fires_once_then_resets(tmp_path, caplog):
    # Weather is on (default priority keeps "morning") but neither the hub nor
    # the local config supplies a location: warn once per state change.
    cfg = Config()  # no coordinates
    client = _HubClient({})
    sources = LiveSources(cfg, client, cache_dir=tmp_path, clock=lambda: MORNING)

    with caplog.at_level(logging.WARNING, logger="nightstand.app"):
        for _ in range(5):
            assert sources.forecast(MORNING, cfg) is None

    warnings = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "no location is set" in r.message
    ]
    assert len(warnings) == 1

    # A location appears (the hub sets a city): the forecast succeeds and the
    # warning state resets, so a later outage warns afresh. pinned_weather forces
    # the fetch regardless of the window.
    sources._client = _ForecastClient()
    got = sources.forecast(
        MORNING, cfg, weather_location=_hub_location(), pinned_weather=True
    )
    assert got is not None

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="nightstand.app"):
        assert sources.forecast(MORNING, cfg) is None
    assert any("no location is set" in r.message for r in caplog.records)


def test_no_location_warning_silent_when_weather_is_off(tmp_path, caplog):
    # When morning weather is disabled there is nothing to nag about, even with
    # no location set.
    cfg = Config()
    off = replace(
        cfg,
        schedule=replace(
            cfg.schedule,
            priority=tuple(n for n in cfg.schedule.priority if n != "morning"),
        ),
    )
    sources = LiveSources(
        cfg, _HubClient({}), cache_dir=tmp_path, clock=lambda: MORNING
    )

    with caplog.at_level(logging.WARNING, logger="nightstand.app"):
        for _ in range(3):
            assert sources.forecast(MORNING, off) is None

    assert not [r for r in caplog.records if "no location is set" in r.message]


def sources_fetch_control(client: httpx.Client) -> FrameControl:
    from nightstand.sources import framecontrol

    control = framecontrol.fetch_control(client, "house.local")
    assert control is not None
    return control


# --- Polling policy (pure functions) ----------------------------------------


def _policy_config() -> Config:
    # Weekday morning window 06:00-08:30, in a fixed zone for determinism.
    return Config(location=Location(timezone="UTC"))


def test_forecast_due_only_from_30_minutes_before_the_window():
    cfg = _policy_config()
    day = datetime(2026, 11, 6, tzinfo=UTC)  # a Friday

    before = day.replace(hour=5, minute=0)
    lead = day.replace(hour=5, minute=30)
    inside = day.replace(hour=7, minute=0)
    at_end = day.replace(hour=8, minute=30)

    assert forecast_due(before, cfg, None) is False
    assert forecast_due(lead, cfg, None) is True
    assert forecast_due(inside, cfg, None) is True
    assert forecast_due(at_end, cfg, None) is False  # window end is exclusive


def test_forecast_due_throttles_to_30_minutes_inside_the_window():
    cfg = _policy_config()
    seven = datetime(2026, 11, 6, 7, 0, tzinfo=UTC)

    assert forecast_due(seven, cfg, seven - timedelta(minutes=10)) is False
    assert forecast_due(seven, cfg, seven - timedelta(minutes=30)) is True


def test_schedule_due_every_15_minutes_and_on_day_rollover():
    base = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
    day = date(2026, 11, 6)

    assert schedule_due(base, day, None, None) is True
    assert schedule_due(base, day, base - timedelta(minutes=10), day) is False
    assert schedule_due(base, day, base - timedelta(minutes=15), day) is True
    # A new local day forces a poll even if the last was under 15 minutes ago.
    midnight = datetime(2026, 11, 7, 0, 0, tzinfo=UTC)
    assert schedule_due(
        midnight, date(2026, 11, 7), midnight - timedelta(minutes=5), day
    ) is True


def test_feed_due_only_while_live():
    live = mlb.parse_game(
        json.loads((FIXTURES / "mlb" / "feed_scoring_play.json").read_text())
    )
    final = mlb.parse_game(
        json.loads((FIXTURES / "mlb" / "feed_final.json").read_text())
    )
    assert live is not None and live.status == "live"
    assert final is not None and final.status == "final"

    assert feed_due(None) is True
    assert feed_due(live) is True
    assert feed_due(final) is False


# --- Polling policy over a simulated day (counting real requests) -----------


class _Clock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)


_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
_SCHEDULE_URL = "https://statsapi.mlb.com/api/v1/schedule"
_FEED_PREFIX = "https://statsapi.mlb.com/api/v1.1/game/"

_LIVE_SCHEDULE = {
    "dates": [
        {
            "games": [
                {
                    "gamePk": 777,
                    "status": {
                        "abstractGameState": "Live",
                        "detailedState": "In Progress",
                    },
                    "gameDate": "2026-11-06T18:00:00Z",
                }
            ]
        }
    ]
}


def _feed(state: str) -> dict:
    return {
        "gamePk": 777,
        "gameData": {
            "status": {"abstractGameState": state},
            "teams": {
                "away": {"id": 1, "abbreviation": "AWY"},
                "home": {"id": 2, "abbreviation": "HOM"},
            },
            "datetime": {"dateTime": "2026-11-06T18:00:00Z"},
        },
        "liveData": {
            "linescore": {
                "currentInning": 3,
                "inningState": "Top",
                "teams": {"away": {"runs": 1}, "home": {"runs": 0}},
                "innings": [
                    {"num": 1, "away": {"runs": 0}, "home": {"runs": 0}},
                    {"num": 2, "away": {"runs": 1}, "home": {"runs": 0}},
                    {"num": 3, "away": {"runs": 0}, "home": {"runs": 0}},
                ],
            },
            "plays": {"scoringPlays": [], "allPlays": []},
        },
    }


class _CountingClient:
    """Serves the forecast, MLB schedule, and feed; records each GET's time."""

    def __init__(self, clock: _Clock, schedule: dict, *, feed_state: str = "Live"):
        self._clock = clock
        self._schedule = schedule
        self.feed_state = feed_state
        self.forecast_at: list[datetime] = []
        self.schedule_at: list[datetime] = []
        self.feed_at: list[datetime] = []
        forecast_path = FIXTURES / "openmeteo" / "forecast.json"
        self._forecast_payload = json.loads(forecast_path.read_text())

    def get(self, url, params=None, timeout=None):
        if url == _FORECAST_URL:
            self.forecast_at.append(self._clock())
            return _Response(self._forecast_payload)
        if url == _SCHEDULE_URL:
            self.schedule_at.append(self._clock())
            return _Response(self._schedule)
        if url.startswith(_FEED_PREFIX):
            self.feed_at.append(self._clock())
            return _Response(_feed(self.feed_state))
        raise AssertionError(f"unexpected GET {url}")


def _min_gap_minutes(times: list[datetime]) -> float:
    gaps = [
        (b - a).total_seconds() / 60 for a, b in zip(times, times[1:], strict=False)
    ]
    return min(gaps) if gaps else float("inf")


def test_no_game_day_throttles_forecast_and_schedule(tmp_path):
    clock = _Clock(datetime(2026, 11, 6, 0, 0, tzinfo=UTC))  # Friday 00:00
    cfg = Config(
        location=Location(latitude=32.7, longitude=-117.2, timezone="UTC"),
    )
    client = _CountingClient(clock, schedule={"dates": []})
    sources = LiveSources(cfg, client, cache_dir=tmp_path, clock=clock)

    for _ in range(24 * 60):  # one cycle a minute for 24 hours
        sources.forecast()
        sources.game()
        clock.advance(60)

    # Open-Meteo is hit only inside the fetch span (05:30-08:30) and at most
    # every 30 minutes.
    assert client.forecast_at, "the forecast should be fetched in the morning"
    for when in client.forecast_at:
        minutes = when.hour * 60 + when.minute
        assert 5 * 60 + 30 <= minutes < 8 * 60 + 30
    assert _min_gap_minutes(client.forecast_at) >= 30

    # The MLB schedule is polled at most every 15 minutes, and there is no game,
    # so the live feed is never fetched.
    assert _min_gap_minutes(client.schedule_at) >= 15
    assert client.feed_at == []


def test_live_game_fetches_feed_every_cycle_until_final(tmp_path):
    clock = _Clock(datetime(2026, 11, 6, 18, 0, tzinfo=UTC))
    cfg = Config(location=Location(timezone="UTC"))
    client = _CountingClient(clock, schedule=_LIVE_SCHEDULE, feed_state="Live")
    sources = LiveSources(cfg, client, cache_dir=tmp_path, clock=clock)

    # Five live cycles: the feed is fetched each time.
    for _ in range(5):
        state = sources.game()
        assert state is not None and state.status == "live"
        clock.advance(60)
    assert len(client.feed_at) == 5

    # The game ends. This cycle still fetches (and observes Final); later cycles
    # must not fetch the feed again.
    client.feed_state = "Final"
    state = sources.game()
    assert state is not None and state.status == "final"
    assert len(client.feed_at) == 6

    for _ in range(5):
        clock.advance(60)
        state = sources.game()
        assert state is not None and state.status == "final"
    assert len(client.feed_at) == 6  # no feed calls after Final

    # The schedule itself was polled at most once (well under 15 minutes total).
    assert len(client.schedule_at) == 1


def _held_records(caplog):
    return [r for r in caplog.records if "redraw held" in r.message]


def _pushed_records(caplog):
    return [r for r in caplog.records if r.message.startswith("pushed ")]


def test_push_logs_screen_detail_and_mode(caplog):
    cfg = _loop_config()
    sources = FakeSources()
    sources.np = NowPlaying("Song", "Artist")
    display = RecordingDisplay()
    state = AppState(shown_identity=("Song", "Artist", None))

    with caplog.at_level(logging.INFO, logger="nightstand.app"):
        assert run_once(state, cfg, sources, display, NOON) is True

    pushed = _pushed_records(caplog)
    assert len(pushed) == 1
    message = pushed[0].getMessage()
    assert "now_playing" in message
    assert "Song" in message
    assert "scheduled" in message


def test_forced_art_push_logs_forced_and_piece_id(tmp_path, caplog):
    cfg = _loop_config()
    art_path = tmp_path / "piece.png"
    Image.new("RGB", (400, 300), PURE["green"]).save(art_path)
    sources = FakeSources()
    sources.art_piece = ArtPiece(
        path=art_path, title="Title", artist="Artist", id="Photos/piece.png"
    )
    sources.art_forced = True
    display = RecordingDisplay()

    with caplog.at_level(logging.INFO, logger="nightstand.app"):
        assert run_once(AppState(), cfg, sources, display, NOON) is True

    pushed = _pushed_records(caplog)
    assert len(pushed) == 1
    message = pushed[0].getMessage()
    assert "art" in message
    assert "Photos/piece.png" in message
    assert "forced" in message


def test_hold_for_settling_logs_once_per_change(caplog):
    cfg = _loop_config()
    sources = FakeSources()
    sources.np = NowPlaying("Song", "Artist")
    display = RecordingDisplay()
    state = AppState()

    with caplog.at_level(logging.INFO, logger="nightstand.app"):
        # A new track is held for the settle; repeated cycles must not re-log it.
        for offset in (0, 5, 10):
            when = NOON + timedelta(seconds=offset)
            assert run_once(state, cfg, sources, display, when) is False

    held = _held_records(caplog)
    assert len(held) == 1
    assert "settling" in held[0].getMessage()
    assert display.shown == []


def test_hold_for_quiet_hours_logs_once(caplog):
    cfg = _loop_config(quiet_start="00:00", quiet_end="23:59")
    sources = FakeSources()
    sources.np = NowPlaying("Song", "Artist")
    display = RecordingDisplay()
    # Already the shown identity, so the settle does not mask quiet-hours.
    state = AppState(shown_identity=("Song", "Artist", None))

    later = NOON + timedelta(seconds=10)
    with caplog.at_level(logging.INFO, logger="nightstand.app"):
        assert run_once(state, cfg, sources, display, NOON) is False
        assert run_once(state, cfg, sources, display, later) is False

    held = _held_records(caplog)
    assert len(held) == 1
    assert "quiet hours" in held[0].getMessage()
    assert display.shown == []


def test_steady_state_logs_no_held_after_push(caplog):
    cfg = _loop_config()
    sources = FakeSources()
    sources.np = NowPlaying("Song", "Artist")
    display = RecordingDisplay()
    state = AppState(shown_identity=("Song", "Artist", None))

    with caplog.at_level(logging.INFO, logger="nightstand.app"):
        assert run_once(state, cfg, sources, display, NOON) is True
        # Nothing changed: no push, and no "held" spam either.
        for offset in (10, 20):
            when = NOON + timedelta(seconds=offset)
            assert run_once(state, cfg, sources, display, when) is False

    assert len(_pushed_records(caplog)) == 1
    assert _held_records(caplog) == []


def test_push_resets_held_so_the_same_hold_logs_again(tmp_path, caplog):
    # A quiet-hours hold on a piece, a forced push of that same piece, then the
    # same hold recurring (here via a health-marker flip, which changes the key
    # but not the piece): the push must clear last_held so the hold logs afresh.
    cfg = _loop_config(quiet_start="00:00", quiet_end="23:59")
    art_path = tmp_path / "piece.png"
    Image.new("RGB", (400, 300), PURE["green"]).save(art_path)
    piece = ArtPiece(path=art_path, title="Title", artist=None, id="met-1.jpg")
    sources = FakeSources()
    sources.art_piece = piece
    display = RecordingDisplay()
    state = AppState()

    with caplog.at_level(logging.INFO, logger="nightstand.app"):
        # Held by quiet hours (first log).
        assert run_once(state, cfg, sources, display, NOON) is False
        # A command forces the same piece onto the panel: this push resets the
        # held memory.
        sources.art_forced = True
        assert run_once(state, cfg, sources, display, NOON) is True
        # Back to scheduled, and a health problem flips the marker so the key
        # differs and the piece is held by quiet hours once more.
        sources.art_forced = False
        sources.health_obj = Health(problems=("nightstand",))
        assert run_once(state, cfg, sources, display, NOON) is False

    held = _held_records(caplog)
    assert len(held) == 2
    assert all("quiet hours" in r.getMessage() for r in held)


def test_held_dedupe_distinguishes_signatures(caplog):
    # Two different tracks settling in a row log twice: the dedupe keys on the
    # signature, not just the reason, so a constant signature would drop one.
    cfg = _loop_config()
    sources = FakeSources()
    sources.np = NowPlaying("Song A", "Artist")
    display = RecordingDisplay()
    state = AppState()

    with caplog.at_level(logging.INFO, logger="nightstand.app"):
        assert run_once(state, cfg, sources, display, NOON) is False
        sources.np = NowPlaying("Song B", "Artist")
        later = NOON + timedelta(seconds=5)
        assert run_once(state, cfg, sources, display, later) is False

    held = _held_records(caplog)
    assert len(held) == 2
    assert all("settling" in r.getMessage() for r in held)


def test_held_dedupe_distinguishes_reasons(caplog):
    # The same track, first held for the settle and then (once settled) by quiet
    # hours, logs both: the reason is part of the dedupe key.
    cfg = _loop_config(quiet_start="00:00", quiet_end="23:59")
    sources = FakeSources()
    sources.np = NowPlaying("Song", "Artist")
    display = RecordingDisplay()
    state = AppState()

    with caplog.at_level(logging.INFO, logger="nightstand.app"):
        assert run_once(state, cfg, sources, display, NOON) is False
        settled = NOON + timedelta(seconds=20)
        assert run_once(state, cfg, sources, display, settled) is False

    held = [r.getMessage() for r in _held_records(caplog)]
    assert len(held) == 2
    assert "settling" in held[0]
    assert "quiet hours" in held[1]


def test_quiet_hold_bucket_advance_logs_once(caplog):
    # A long-form track held through quiet hours: its progress bucket advances,
    # changing the redraw key, but the hold is on the track identity, so it is
    # logged once, not on every bucket.
    cfg = _loop_config(quiet_start="00:00", quiet_end="23:59")
    lf = _long_form(position=0.0, position_at=NOON)
    sources = FakeSources()
    sources.control = _frame_control(kinds=("music", "podcast"))
    sources.np = lf
    display = RecordingDisplay()
    # Already the shown identity, so the settle does not mask the quiet hold.
    state = AppState(shown_identity=(lf.title, lf.artist, lf.album))

    with caplog.at_level(logging.INFO, logger="nightstand.app"):
        assert run_once(state, cfg, sources, display, NOON) is False
        # Eleven minutes later the 600 s bucket has advanced.
        later = NOON + timedelta(minutes=11)
        assert run_once(state, cfg, sources, display, later) is False

    held = _held_records(caplog)
    assert len(held) == 1
    assert "quiet hours" in held[0].getMessage()


# --- Frame control: the kinds filter, settings, and pins --------------------


def _a_forecast():
    data = json.loads((FIXTURES / "openmeteo" / "forecast.json").read_text())
    return weather_source.parse_forecast(data, MORNING)


def _art_piece(tmp_path, piece_id="x.png"):
    path = tmp_path / piece_id
    Image.new("RGB", (400, 300), PURE["green"]).save(path)
    return ArtPiece(path=path, title="Title", id=piece_id)


def test_kinds_filter_keeps_a_disabled_category_off_the_frame(tmp_path, caplog):
    # A podcast (long-form, no genre) with music-only kinds does not take the
    # frame: the art screen shows instead, as if nothing were playing.
    cfg = _loop_config()
    sources = FakeSources()
    sources.np = NowPlaying("Episode 1", "Show", kind="long_form")
    sources.art_piece = _art_piece(tmp_path)
    sources.control = _frame_control(kinds=("music",))
    display = RecordingDisplay()

    with caplog.at_level(logging.INFO, logger="nightstand.app"):
        assert run_once(AppState(), cfg, sources, display, NOON) is True

    message = _pushed_records(caplog)[0].getMessage()
    assert "art" in message
    assert "now_playing" not in message


def test_now_playing_pin_shows_a_disabled_kind(tmp_path, caplog):
    # A now-playing pin shows any kind, overriding the kinds filter.
    cfg = _loop_config()
    sources = FakeSources()
    sources.np = NowPlaying("Episode 1", "Show", kind="long_form")
    sources.control = _frame_control(
        kinds=("music",), pin=Pin("now_playing", None, None, 1, None)
    )
    display = RecordingDisplay()
    state = AppState(shown_identity=("Episode 1", "Show", None))

    with caplog.at_level(logging.INFO, logger="nightstand.app"):
        assert run_once(state, cfg, sources, display, NOON) is True

    assert "now_playing" in _pushed_records(caplog)[0].getMessage()
    # The pin targets now playing, so neither art nor weather is pinned.
    assert sources.art_pinned_piece is None
    assert sources.forecast_pinned_weather is False


def test_morning_weather_disabled_via_hub_drops_the_weather_screen(tmp_path, caplog):
    cfg = _loop_config()
    sources = FakeSources()
    sources.forecast_obj = _a_forecast()
    sources.art_piece = _art_piece(tmp_path)
    sources.control = _frame_control(
        morning=MorningWeather(
            enabled=False, weekday=("06:00", "08:30"), weekend=("07:00", "09:00")
        )
    )
    display = RecordingDisplay()

    with caplog.at_level(logging.INFO, logger="nightstand.app"):
        # MORNING is inside the default weekday window, but the hub disabled it.
        assert run_once(AppState(), cfg, sources, display, MORNING) is True

    message = _pushed_records(caplog)[0].getMessage()
    assert "art" in message
    assert "weather" not in message


def test_quiet_hours_from_the_hub_suppress_a_push():
    # The local config has no quiet hours; the hub's quiet window governs.
    cfg = _loop_config()  # local quiet [00:00, 00:00) never matches
    sources = FakeSources()
    sources.np = NowPlaying("Song", "Artist")
    sources.control = _frame_control(quiet=("00:00", "23:59"))
    display = RecordingDisplay()
    state = AppState(shown_identity=("Song", "Artist", None))

    assert run_once(state, cfg, sources, display, NOON) is False
    assert display.shown == []


def test_art_pin_forwards_the_piece_to_the_source(tmp_path):
    cfg = _loop_config()
    sources = FakeSources()
    sources.art_piece = _art_piece(tmp_path)
    sources.control = _frame_control(pin=Pin("art", "x.png", None, 1, None))

    run_once(AppState(), cfg, sources, RecordingDisplay(), NOON)

    assert sources.art_pinned_piece == "x.png"


def test_pin_set_during_quiet_hours_redraws_exactly_once(tmp_path):
    # Review Focus 2: a pin set during quiet hours forces one redraw, then holds.
    cfg = _loop_config(quiet_start="00:00", quiet_end="23:59")
    sources = FakeSources()
    sources.art_piece = _art_piece(tmp_path)
    display = RecordingDisplay()
    state = AppState()

    # The hub is serving a control with no pin: the art is held by quiet hours
    # and the startup grace is spent on this first control seen.
    sources.control = _frame_control()
    assert run_once(state, cfg, sources, display, NOON) is False
    # A pin appears: it forces one redraw despite quiet hours.
    sources.control = _frame_control(pin=Pin("art", "x.png", None, 1, None))
    assert run_once(state, cfg, sources, display, NOON) is True
    # The same pin on the next cycle does not redraw again.
    assert run_once(state, cfg, sources, display, NOON) is False
    assert len(display.shown) == 1


def test_expiring_pin_returns_to_the_schedule_without_a_storm(tmp_path):
    # Review Focus 2: an expiring pin redraws the normal screen once, not a storm.
    cfg = _loop_config()  # quiet never matches
    sources = FakeSources()
    sources.forecast_obj = _a_forecast()
    sources.art_piece = _art_piece(tmp_path)
    until = NOON + timedelta(minutes=5)
    sources.control = _frame_control(pin=Pin("weather", None, until, 1, None))
    display = RecordingDisplay()
    state = AppState()

    # Weather is pinned: it draws once and the forecast is pinned-fetched.
    assert run_once(state, cfg, sources, display, NOON) is True
    assert sources.forecast_pinned_weather is True
    # Still pinned, same key: no redraw.
    assert run_once(state, cfg, sources, display, NOON + timedelta(minutes=1)) is False

    # The pin expires: the normal schedule (art) redraws exactly once.
    expired = NOON + timedelta(minutes=6)
    assert run_once(state, cfg, sources, display, expired) is True
    assert sources.forecast_pinned_weather is False
    for i in range(5):
        when = expired + timedelta(seconds=10 * (i + 1))
        assert run_once(state, cfg, sources, display, when) is False
    assert len(display.shown) == 2


def test_pin_evaluation_that_raises_keeps_the_loop_running(tmp_path, caplog):
    # Review Focus 1, second line of defense: a bad pin (here a naive `until`
    # built directly, bypassing the parser) makes the aware/naive comparison in
    # active_pin raise. The loop must not die: the pin is ignored and the
    # schedule still draws, with the failure logged once per outage.
    cfg = _loop_config()
    sources = FakeSources()
    sources.art_piece = _art_piece(tmp_path)
    naive_until = datetime(2026, 11, 6, 7, 0, 0)  # no tzinfo: naive
    sources.control = FrameControl(
        settings=FrameSettings(
            now_playing_kinds=("music",), quiet_hours=None, morning_weather=None
        ),
        pin=Pin(what="art", piece="x.png", until=naive_until, seq=1, set_at=None),
    )
    display = RecordingDisplay()
    state = AppState()

    with caplog.at_level(logging.WARNING, logger="nightstand.app"):
        assert run_once(state, cfg, sources, display, NOON) is True
        # A second failing cycle degrades the same way but does not re-log.
        run_once(state, cfg, sources, display, NOON)

    assert len(display.shown) >= 1
    assert display.shown[-1].size == (WIDTH, HEIGHT)
    warnings = [
        r for r in caplog.records if "pin evaluation failed" in r.getMessage()
    ]
    assert len(warnings) == 1


def test_startup_grace_baseline_is_the_first_control_seen(tmp_path):
    # Minor 1: if the hub is unreachable on the first cycle (no control), the
    # grace is not spent; the first control that then appears with a pin is the
    # baseline and does not force a redraw, so a pin pre-existing at a reboot does
    # not wake the panel at night.
    cfg = _loop_config(quiet_start="00:00", quiet_end="23:59")  # quiet all day
    sources = FakeSources()
    sources.art_piece = _art_piece(tmp_path)
    display = RecordingDisplay()
    state = AppState()

    # Cycle 1: hub down, no control at all.
    sources.control = None
    assert run_once(state, cfg, sources, display, NOON) is False

    # Cycle 2: the first control seen carries a pin; it is a baseline, not a
    # change, so it does not force a redraw through quiet hours.
    sources.control = _frame_control(pin=Pin("art", "x.png", None, 1, None))
    assert run_once(state, cfg, sources, display, NOON) is False
    assert display.shown == []


def test_pin_still_forces_after_a_settle_held_cycle(tmp_path):
    # A pin set on the same cycle a new track is still settling must not lose its
    # forced redraw: the settle holds that cycle, and the pin forces once settled.
    cfg = _loop_config(quiet_start="00:00", quiet_end="23:59")  # quiet all day
    sources = FakeSources()
    sources.forecast_obj = _a_forecast()
    sources.art_piece = _art_piece(tmp_path)
    display = RecordingDisplay()
    # The frame has been running: a music track is already shown.
    state = AppState(shown_identity=("Old", "Band", None), pin_seen=True)
    sources.np = NowPlaying("Old", "Band")

    # A new track starts and the weather is pinned on the same cycle. The new
    # identity is held by the settle, so this cycle draws nothing.
    sources.np = NowPlaying("New", "Band")
    sources.control = _frame_control(pin=Pin("weather", None, None, 1, None))
    assert run_once(state, cfg, sources, display, NOON) is False
    assert display.shown == []

    # Once the track settles, the pin forces the weather redraw despite quiet
    # hours: the held cycle did not swallow the pin's one forced redraw.
    settled = NOON + timedelta(seconds=20)
    assert run_once(state, cfg, sources, display, settled) is True
    assert len(display.shown) == 1


def test_live_game_wins_over_a_pin(tmp_path, caplog):
    cfg = _loop_config()
    feed = json.loads((FIXTURES / "mlb" / "feed_scoring_play.json").read_text())
    sources = FakeSources()
    sources.game_state = mlb.parse_game(feed)
    sources.art_piece = _art_piece(tmp_path)
    sources.logo_img = Image.new("RGB", (150, 150), PURE["blue"])
    sources.control = _frame_control(pin=Pin("art", "x.png", None, 1, None))
    display = RecordingDisplay()

    with caplog.at_level(logging.INFO, logger="nightstand.app"):
        assert run_once(AppState(), cfg, sources, display, NOON) is True

    assert "game" in _pushed_records(caplog)[0].getMessage()


# --- LiveSources: frame control wiring --------------------------------------


def _landscape_art_dir(tmp_path, names):
    art_dir = tmp_path / "art"
    art_dir.mkdir()
    for name in names:
        Image.new("RGB", (300, 200), PURE["green"]).save(art_dir / name)
    return art_dir


def _no_hub(request: httpx.Request) -> httpx.Response:
    # No art-control and no frame-control: the frame falls back to local config.
    return httpx.Response(404)


def test_live_sources_frame_control_parsed_from_the_hub(tmp_path):
    doc = {
        "settings": {
            "now_playing_kinds": ["music", "podcast"],
            "quiet_hours": {"start": "23:00", "end": "05:00"},
            "morning_weather": None,
        },
        "pin": {
            "what": "weather", "piece": None, "until": None, "seq": 2, "set_at": None
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/frame-control.json":
            return httpx.Response(200, json=doc)
        raise AssertionError(f"unexpected request {request.url}")

    cfg = Config(location=Location(timezone="UTC"))
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        sources = LiveSources(cfg, client, cache_dir=tmp_path, clock=lambda: NOON)
        control = sources.frame_control()

    assert control is not None
    assert control.settings.now_playing_kinds == ("music", "podcast")
    assert control.pin is not None and control.pin.what == "weather"


def test_live_sources_frame_control_missing_file_is_none(tmp_path):
    cfg = Config(location=Location(timezone="UTC"))
    with httpx.Client(transport=httpx.MockTransport(_no_hub)) as client:
        sources = LiveSources(cfg, client, cache_dir=tmp_path, clock=lambda: NOON)
        assert sources.frame_control() is None


def test_live_sources_art_holds_the_pinned_piece_without_advancing(tmp_path):
    art_dir = _landscape_art_dir(tmp_path, ("a.png", "b.png", "c.png"))
    cfg = Config(art=Art(dir=art_dir), location=Location(timezone="UTC"))

    with httpx.Client(transport=httpx.MockTransport(_no_hub)) as client:
        sources = LiveSources(
            cfg, client, cache_dir=tmp_path, state_dir=tmp_path / "state",
            clock=lambda: NOON,
        )
        # Establish the rotation on its first piece.
        first = sources.art(NOON)
        sources.commit_art(NOON)
        first_id = first.piece.id

        pinned_id = next(
            name for name in ("a.png", "b.png", "c.png") if name != first_id
        )
        view = sources.art(NOON, pinned_id)
        assert view.piece is not None and view.piece.id == pinned_id
        assert view.forced is False

        state = json.loads((tmp_path / "state" / "rotation.json").read_text())
        assert state["current_id"] == first_id  # rotation did not advance

        # Across a full interval the pinned piece still holds and does not advance.
        later = NOON + timedelta(hours=2)
        view_later = sources.art(later, pinned_id)
        assert view_later.piece.id == pinned_id
        state_later = json.loads((tmp_path / "state" / "rotation.json").read_text())
        assert state_later["current_id"] == first_id


def test_live_sources_art_pin_falls_back_when_the_piece_is_gone(tmp_path):
    # Review Focus 4: the pinned piece was deleted from the library; the frame
    # falls back to the normal schedule rather than crashing or showing nothing.
    art_dir = _landscape_art_dir(tmp_path, ("only.png",))
    cfg = Config(art=Art(dir=art_dir), location=Location(timezone="UTC"))

    with httpx.Client(transport=httpx.MockTransport(_no_hub)) as client:
        sources = LiveSources(
            cfg, client, cache_dir=tmp_path, state_dir=tmp_path / "state",
            clock=lambda: NOON,
        )
        view = sources.art(NOON, "deleted.png")

    assert view.piece is not None
    assert view.piece.id == "only.png"


def test_live_sources_forecast_fetched_every_30_minutes_while_weather_pinned(tmp_path):
    clock = _Clock(datetime(2026, 11, 6, 0, 0, tzinfo=UTC))
    cfg = Config(location=Location(latitude=32.7, longitude=-117.2, timezone="UTC"))
    client = _CountingClient(clock, schedule={"dates": []})
    sources = LiveSources(cfg, client, cache_dir=tmp_path, clock=clock)

    for _ in range(24 * 60):  # one cycle a minute for 24 hours
        sources.forecast(clock(), cfg, pinned_weather=True)
        clock.advance(60)

    # The forecast is pinned-fetched all day (not only the morning), every 30 min.
    assert _min_gap_minutes(client.forecast_at) >= 30
    assert any(when.hour >= 12 for when in client.forecast_at)  # afternoon too
    assert len(client.forecast_at) >= 40  # about 48 over the day
