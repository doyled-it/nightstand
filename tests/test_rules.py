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
