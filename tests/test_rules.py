"""Tests for the pure rules engine.

Every case passes an aware ``now`` and a config whose location timezone is fixed
to ``America/Los_Angeles`` so the quiet-hours and morning windows resolve the
same way on any machine, independent of the host's system zone.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from nightstand import rules
from nightstand.config import Art, Config, Game, Schedule
from nightstand.models import ArtPiece, NowPlaying
from nightstand.rules import Context
from nightstand.sources.framecontrol import (
    QUIET_HOURS_OFF,
    FrameControl,
    FrameSettings,
    MorningWeather,
    Pin,
    WeatherLocation,
)
from nightstand.sources.mlb import GameState, Team
from nightstand.sources.weather import Forecast

LA = "America/Los_Angeles"


def make_config(
    *,
    schedule: Schedule | None = None,
    game: Game | None = None,
    art: Art | None = None,
) -> Config:
    base = Config()
    return replace(
        base,
        location=replace(base.location, timezone=LA),
        schedule=schedule or base.schedule,
        game=game or base.game,
        art=art or base.art,
    )


def la(year, month, day, hour, minute):
    from zoneinfo import ZoneInfo

    return datetime(year, month, day, hour, minute, tzinfo=ZoneInfo(LA))


def live_game() -> GameState:
    return GameState(
        game_pk=1,
        status="live",
        away=Team(id=133, abbrev="ATH", runs=2),
        home=Team(id=135, abbrev="SD", runs=3),
        inning=5,
        inning_state="Top",
        summary="",
        ended_at=None,
    )


def final_game(ended_at: datetime) -> GameState:
    return GameState(
        game_pk=1,
        status="final",
        away=Team(id=133, abbrev="ATH", runs=2),
        home=Team(id=135, abbrev="SD", runs=3),
        inning=9,
        inning_state="End",
        summary="Final: SD win 3-2",
        ended_at=ended_at,
    )


def a_forecast() -> Forecast:
    return Forecast(
        now_temp=60,
        description="Clear",
        high=70,
        low=50,
        hours=(),
        sunrise=None,
        sunset=None,
        uv_peak=3,
        uv_peak_time=None,
    )


def an_art_piece() -> ArtPiece:
    return ArtPiece(path=Path("/art/coastal.jpg"), title="Coastal", id="coastal.jpg")


def a_track() -> NowPlaying:
    return NowPlaying(
        title="Song",
        artist="Band",
        album="Album",
        rooms=("Office",),
    )


# --- morning window -----------------------------------------------------------


def test_weekday_six_am_is_morning_but_five_fifty_nine_is_not():
    cfg = make_config()
    # 2026-11-09 is a Monday.
    assert rules.in_morning_window(la(2026, 11, 9, 6, 0), cfg) is True
    assert rules.in_morning_window(la(2026, 11, 9, 5, 59), cfg) is False


def test_saturday_window_starts_at_seven():
    cfg = make_config()
    # 2026-11-07 is a Saturday; the weekend window opens at 07:00.
    assert rules.in_morning_window(la(2026, 11, 7, 6, 30), cfg) is False
    assert rules.in_morning_window(la(2026, 11, 7, 7, 0), cfg) is True


def test_morning_window_is_stable_across_dst_transitions():
    # Both US DST switch days are Sundays, so widen the weekend window to open at
    # 06:00. Each ``now`` below is the UTC instant of 06:00 local on that day, so
    # a correct local-time conversion must land inside the window despite the
    # changing UTC offset (PDT -07:00 vs PST -08:00).
    cfg = make_config(schedule=replace(Schedule(), morning_weekend=("06:00", "09:00")))
    dst_start = datetime(2027, 3, 14, 13, 0, tzinfo=UTC)  # 06:00 PDT
    dst_end = datetime(2026, 11, 1, 14, 0, tzinfo=UTC)  # 06:00 PST
    assert rules.in_morning_window(dst_start, cfg) is True
    assert rules.in_morning_window(dst_end, cfg) is True


# --- quiet hours --------------------------------------------------------------


def test_quiet_hours_wrap_midnight():
    cfg = make_config()
    assert rules.in_quiet_hours(la(2026, 11, 9, 22, 0), cfg) is True
    assert rules.in_quiet_hours(la(2026, 11, 9, 5, 59), cfg) is True
    assert rules.in_quiet_hours(la(2026, 11, 9, 6, 0), cfg) is False


# --- priority -----------------------------------------------------------------


def test_music_outranks_game_by_default_and_swapping_flips_it():
    ctx = Context(now_playing=a_track(), game=live_game())
    now = la(2026, 11, 9, 14, 0)  # Monday afternoon: not morning, not quiet.

    default = make_config()
    assert rules.decide(now, default, ctx).screen == "now_playing"

    swapped = make_config(
        schedule=replace(Schedule(), priority=("morning", "game", "music", "art"))
    )
    assert rules.decide(now, swapped, ctx).screen == "game"


def test_empty_context_gives_placeholder():
    cfg = make_config()
    decision = rules.decide(la(2026, 11, 9, 14, 0), cfg, Context())
    assert decision.screen == "placeholder"
    assert decision.redraw_key == ("placeholder",)


def test_morning_with_forecast_shows_weather_and_split_with_music():
    cfg = make_config()
    now = la(2026, 11, 9, 6, 30)  # inside the weekday morning window.

    weather_only = rules.decide(now, cfg, Context(forecast=a_forecast()))
    assert weather_only.screen == "weather"

    with_music = rules.decide(
        now, cfg, Context(forecast=a_forecast(), now_playing=a_track())
    )
    assert with_music.screen == "split"


# --- game hold and quiet-hours push ------------------------------------------


def test_live_game_pushes_at_night_while_art_does_not():
    cfg = make_config()
    now = la(2026, 11, 9, 22, 30)  # quiet hours.

    game_decision = rules.decide(now, cfg, Context(game=live_game()))
    assert game_decision.screen == "game"
    assert rules.should_push(game_decision, None, now, cfg) is True

    art_decision = rules.decide(now, cfg, Context(art=an_art_piece()))
    assert art_decision.screen == "art"
    assert rules.should_push(art_decision, None, now, cfg) is False


def test_final_game_holds_for_an_hour_then_falls_to_art():
    cfg = make_config()
    now = la(2026, 11, 9, 14, 0)

    fresh = Context(game=final_game(now - timedelta(minutes=59)), art=an_art_piece())
    assert rules.decide(now, cfg, fresh).screen == "game"

    stale = Context(game=final_game(now - timedelta(minutes=61)), art=an_art_piece())
    assert rules.decide(now, cfg, stale).screen == "art"


# --- quiet-hours game override ------------------------------------------------


def test_quiet_hours_music_and_live_game_lets_the_game_push():
    cfg = make_config()
    now = la(2026, 11, 9, 22, 30)  # quiet hours.
    ctx = Context(now_playing=a_track(), game=live_game())

    decision = rules.decide(now, cfg, ctx)

    assert decision.screen == "game"
    assert rules.should_push(decision, None, now, cfg) is True


def test_quiet_hours_music_and_final_game_in_hold_lets_the_game_push():
    cfg = make_config()
    now = la(2026, 11, 9, 22, 30)  # quiet hours.
    ctx = Context(
        now_playing=a_track(), game=final_game(now - timedelta(minutes=30))
    )

    decision = rules.decide(now, cfg, ctx)

    assert decision.screen == "game"
    assert rules.should_push(decision, None, now, cfg) is True


def test_quiet_hours_music_and_no_game_pushes_nothing():
    cfg = make_config()
    now = la(2026, 11, 9, 22, 30)  # quiet hours.
    ctx = Context(now_playing=a_track())

    decision = rules.decide(now, cfg, ctx)

    assert decision.screen == "now_playing"
    assert rules.should_push(decision, None, now, cfg) is False


def test_daytime_music_and_live_game_keeps_default_priority():
    cfg = make_config()
    now = la(2026, 11, 9, 14, 0)  # afternoon: not quiet.
    ctx = Context(now_playing=a_track(), game=live_game())

    decision = rules.decide(now, cfg, ctx)

    assert decision.screen == "now_playing"
    assert rules.should_push(decision, None, now, cfg) is True


# --- push gating --------------------------------------------------------------


def test_unchanged_key_does_not_push():
    cfg = make_config()
    now = la(2026, 11, 9, 14, 0)
    decision = rules.decide(now, cfg, Context(art=an_art_piece()))
    assert rules.should_push(decision, decision.redraw_key, now, cfg) is False


def test_art_key_is_the_current_piece_id():
    cfg = make_config()
    now = la(2026, 11, 9, 14, 0)
    decision = rules.decide(now, cfg, Context(art=an_art_piece()))
    assert decision.redraw_key == ("art", "coastal.jpg")


def test_forced_decision_pushes_in_quiet_hours_but_scheduled_does_not():
    # A command bypasses quiet hours; scheduled rotation keeps the quiet rule.
    cfg = make_config()
    now = la(2026, 11, 9, 22, 30)  # quiet hours
    scheduled = rules.decide(now, cfg, Context(art=an_art_piece()))
    assert scheduled.screen == "art"
    assert rules.should_push(scheduled, None, now, cfg) is False

    forced = replace(scheduled, forced=True)
    assert rules.should_push(forced, None, now, cfg) is True


def test_forced_decision_still_suppressed_when_key_unchanged():
    cfg = make_config()
    now = la(2026, 11, 9, 22, 30)  # quiet hours
    decision = replace(rules.decide(now, cfg, Context(art=an_art_piece())), forced=True)
    assert rules.should_push(decision, decision.redraw_key, now, cfg) is False


# --- progress bucket ----------------------------------------------------------


def a_long_form(*, status="playing", position=0.0) -> NowPlaying:
    return NowPlaying(
        title="Episode 1",
        artist="Show",
        album=None,
        rooms=("Office",),
        kind="long_form",
        status=status,
        duration=7200.0,
        position=position,
        position_at=la(2026, 11, 9, 14, 0),
    )


def test_long_form_bucket_increments_every_ten_minutes_while_playing():
    cfg = make_config()
    start = la(2026, 11, 9, 14, 0)
    track = a_long_form(position=0.0)

    assert rules._now_playing_key(track, start, cfg)[-1] == 0
    assert rules._now_playing_key(track, start + timedelta(seconds=599), cfg)[-1] == 0
    assert rules._now_playing_key(track, start + timedelta(seconds=600), cfg)[-1] == 1
    assert rules._now_playing_key(track, start + timedelta(seconds=1250), cfg)[-1] == 2


def test_long_form_bucket_stays_fixed_while_paused():
    cfg = make_config()
    start = la(2026, 11, 9, 14, 0)
    # Paused at 1300 s (bucket 2). position_now holds the recorded position, so
    # the bucket must not advance as wall-clock time passes.
    track = a_long_form(status="paused", position=1300.0)

    assert rules._now_playing_key(track, start, cfg)[-1] == 2
    assert rules._now_playing_key(track, start + timedelta(seconds=600), cfg)[-1] == 2


def test_music_track_has_no_bucket():
    cfg = make_config()
    now = la(2026, 11, 9, 14, 0)
    # a_track() is kind="music"; even with a position, music never buckets.
    music = replace(a_track(), position=120.0, position_at=now)

    assert rules._now_playing_key(music, now, cfg)[-1] is None


def test_long_form_without_a_known_position_has_no_bucket():
    cfg = make_config()
    now = la(2026, 11, 9, 14, 0)
    track = replace(a_long_form(), position=None, position_at=None)

    assert rules._now_playing_key(track, now, cfg)[-1] is None


# --- category (Review Focus 3) -----------------------------------------------


def _track(*, kind="music", genre=()) -> NowPlaying:
    return NowPlaying(title="T", artist="A", rooms=("Office",), kind=kind, genre=genre)


def test_long_form_with_no_genre_is_a_podcast():
    assert rules.category(_track(kind="long_form", genre=())) == "podcast"


def test_music_with_no_genre_is_music():
    assert rules.category(_track(kind="music", genre=())) == "music"


def test_audiobook_genre_on_a_short_music_track_is_an_audiobook():
    # A short track (kind music) whose genre names an audiobook is an audiobook,
    # genre deciding over kind.
    assert rules.category(_track(kind="music", genre=("Audiobook",))) == "audiobook"


def test_spoken_word_genre_is_an_audiobook():
    track = _track(kind="long_form", genre=("Spoken Word",))
    assert rules.category(track) == "audiobook"


def test_podcast_genre_is_a_podcast():
    assert rules.category(_track(kind="music", genre=("Podcast",))) == "podcast"


def test_a_plain_music_genre_stays_music():
    assert rules.category(_track(kind="music", genre=("Indie Rock",))) == "music"


# --- the kinds filter --------------------------------------------------------


def test_now_playing_passes_only_an_enabled_category():
    music = _track(kind="music")
    podcast = _track(kind="long_form", genre=("Podcast",))

    assert rules.now_playing_for_frame(music, ("music",), None) is music
    assert rules.now_playing_for_frame(podcast, ("music",), None) is None
    assert rules.now_playing_for_frame(podcast, ("music", "podcast"), None) is podcast


def test_now_playing_pin_passes_any_kind():
    podcast = _track(kind="long_form", genre=("Podcast",))
    pin = Pin(what="now_playing", piece=None, until=None, seq=1, set_at=None)

    # Music-only kinds would normally drop the podcast, but a now_playing pin
    # shows any kind.
    assert rules.now_playing_for_frame(podcast, ("music",), pin) is podcast


def test_filter_passes_none_through():
    assert rules.now_playing_for_frame(None, ("music",), None) is None


# --- active_pin --------------------------------------------------------------


def _control(pin: Pin | None) -> FrameControl:
    settings = FrameSettings(
        now_playing_kinds=("music",), quiet_hours=None, morning_weather=None
    )
    return FrameControl(settings=settings, pin=pin)


def test_active_pin_none_without_control_or_pin():
    now = la(2026, 11, 9, 14, 0)
    assert rules.active_pin(None, now) is None
    assert rules.active_pin(_control(None), now) is None


def test_active_pin_returns_a_live_pin():
    now = la(2026, 11, 9, 14, 0)
    pin = Pin(
        what="art", piece="a.jpg", until=now + timedelta(hours=1), seq=1, set_at=None
    )
    assert rules.active_pin(_control(pin), now) is pin


def test_active_pin_drops_an_expired_pin():
    now = la(2026, 11, 9, 14, 0)
    pin = Pin(
        what="art", piece="a.jpg", until=now - timedelta(minutes=1), seq=1, set_at=None
    )
    assert rules.active_pin(_control(pin), now) is None


def test_active_pin_keeps_a_forever_pin():
    now = la(2026, 11, 9, 14, 0)
    pin = Pin(what="weather", piece=None, until=None, seq=1, set_at=None)
    assert rules.active_pin(_control(pin), now) is pin


# --- effective_config --------------------------------------------------------


def test_effective_config_is_unchanged_without_settings():
    cfg = make_config()
    assert rules.effective_config(cfg, None) is cfg


def test_effective_config_overlays_quiet_hours():
    cfg = make_config()
    settings = FrameSettings(
        now_playing_kinds=("music",),
        quiet_hours=("23:00", "05:00"),
        morning_weather=None,
    )
    eff = rules.effective_config(cfg, settings)
    assert eff.schedule.quiet_start == "23:00"
    assert eff.schedule.quiet_end == "05:00"


def test_quiet_hours_off_window_is_never_quiet():
    # A frame whose quiet hours are off (framecontrol.QUIET_HOURS_OFF, a
    # zero-length window) overlays to a config that is never in quiet hours, even
    # at an hour the local config would call quiet.
    cfg = make_config(
        schedule=replace(Schedule(), quiet_start="22:00", quiet_end="06:00")
    )
    settings = FrameSettings(
        now_playing_kinds=("music",),
        quiet_hours=QUIET_HOURS_OFF,
        morning_weather=None,
    )
    eff = rules.effective_config(cfg, settings)
    assert rules.in_quiet_hours(la(2026, 11, 9, 23, 0), eff) is False
    assert rules.in_quiet_hours(la(2026, 11, 9, 3, 0), eff) is False


def test_effective_config_overlays_the_morning_window():
    cfg = make_config()
    settings = FrameSettings(
        now_playing_kinds=("music",),
        quiet_hours=None,
        morning_weather=MorningWeather(
            enabled=True, weekday=("05:30", "09:00"), weekend=("06:00", "10:00")
        ),
    )
    eff = rules.effective_config(cfg, settings)
    assert eff.schedule.morning_weekday == ("05:30", "09:00")
    assert eff.schedule.morning_weekend == ("06:00", "10:00")
    assert "morning" in eff.schedule.priority


def test_disabling_morning_weather_drops_the_morning_screen():
    cfg = make_config()
    settings = FrameSettings(
        now_playing_kinds=("music",),
        quiet_hours=None,
        morning_weather=MorningWeather(
            enabled=False, weekday=("06:00", "08:30"), weekend=("07:00", "09:00")
        ),
    )
    eff = rules.effective_config(cfg, settings)
    assert "morning" not in eff.schedule.priority
    # The morning window never matches, so no weather shows in the morning.
    now = la(2026, 11, 9, 6, 30)  # inside the default weekday morning window
    decision = rules.decide(now, eff, Context(forecast=a_forecast()))
    assert decision.screen != "weather"


def test_effective_config_leaves_the_schedule_clock_local_for_a_weather_city():
    # The hub's weather city must not reach the schedule clock: effective_config
    # leaves location (and so quiet hours / the morning window) exactly as the
    # local config has it. The forecast-only override lives in LiveSources.forecast.
    cfg = make_config()
    settings = FrameSettings(
        now_playing_kinds=("music",),
        quiet_hours=None,
        morning_weather=None,
        weather_location=WeatherLocation(
            name="London, England, United Kingdom",
            latitude=51.51,
            longitude=-0.13,
            timezone="Europe/London",
        ),
    )
    eff = rules.effective_config(cfg, settings)
    assert eff.location == cfg.location
    assert eff.schedule == cfg.schedule
    # With no schedule changes either, the config is returned unchanged.
    assert eff is cfg


# --- pinned decisions --------------------------------------------------------


def _pin(what, *, piece=None) -> Pin:
    return Pin(what=what, piece=piece, until=None, seq=1, set_at=None)


def test_art_pin_shows_the_pinned_piece():
    cfg = make_config()
    now = la(2026, 11, 9, 14, 0)
    decision = rules.decide(
        now, cfg, Context(art=an_art_piece()), _pin("art", piece="coastal.jpg")
    )
    assert decision.screen == "art"
    assert decision.redraw_key == ("art", "coastal.jpg")


def test_weather_pin_shows_the_weather_screen():
    cfg = make_config()
    now = la(2026, 11, 9, 14, 0)  # not morning, not quiet
    decision = rules.decide(now, cfg, Context(forecast=a_forecast()), _pin("weather"))
    assert decision.screen == "weather"


def test_now_playing_pin_shows_now_playing():
    cfg = make_config()
    now = la(2026, 11, 9, 14, 0)
    decision = rules.decide(
        now, cfg, Context(now_playing=a_track()), _pin("now_playing")
    )
    assert decision.screen == "now_playing"


def test_now_playing_pin_falls_back_to_art_when_nothing_plays():
    cfg = make_config()
    now = la(2026, 11, 9, 14, 0)
    decision = rules.decide(
        now, cfg, Context(art=an_art_piece()), _pin("now_playing")
    )
    assert decision.screen == "art"


def test_a_live_game_wins_over_a_pin():
    cfg = make_config()
    now = la(2026, 11, 9, 14, 0)
    ctx = Context(art=an_art_piece(), game=live_game())
    decision = rules.decide(now, cfg, ctx, _pin("art", piece="coastal.jpg"))
    assert decision.screen == "game"


def test_pin_falls_through_when_its_screen_has_nothing():
    # Weather pinned but no forecast yet: fall through to the normal schedule
    # rather than showing a blank weather screen.
    cfg = make_config()
    now = la(2026, 11, 9, 14, 0)
    decision = rules.decide(now, cfg, Context(art=an_art_piece()), _pin("weather"))
    assert decision.screen == "art"


def test_pinned_art_key_is_stable_so_it_holds_across_intervals():
    # The redraw key for pinned art is just the piece id, so repeated cycles do
    # not redraw: pinned art holds the piece.
    cfg = make_config()
    now = la(2026, 11, 9, 14, 0)
    pin = _pin("art", piece="coastal.jpg")
    d1 = rules.decide(now, cfg, Context(art=an_art_piece()), pin)
    d2 = rules.decide(
        now + timedelta(hours=2), cfg, Context(art=an_art_piece()), pin
    )
    assert d1.redraw_key == d2.redraw_key
