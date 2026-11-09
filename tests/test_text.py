from datetime import datetime

from PIL import Image, ImageDraw

from nightstand.text import clock_time, font, wrap


def _draw():
    return ImageDraw.Draw(Image.new("RGB", (800, 480)))


def test_font_loads_each_weight():
    for weight in ("regular", "bold", "black"):
        f = font(weight, 48)
        assert f.size == 48


def test_wrap_truncates_with_ellipsis():
    lines = wrap(_draw(), "word " * 200, font("bold", 64), 300, 3)
    assert len(lines) == 3 and lines[-1].endswith("…")


def test_wrap_keeps_short_text_on_one_line():
    lines = wrap(_draw(), "hi there", font("regular", 32), 800, 3)
    assert lines == ["hi there"]


def test_wrap_fits_within_max_width():
    draw = _draw()
    f = font("regular", 40)
    lines = wrap(draw, "the quick brown fox jumps over", f, 300, 5)
    assert all(draw.textlength(line, font=f) <= 300 for line in lines)


def test_wrap_ellipsizes_overlong_single_word():
    draw = _draw()
    f = font("bold", 64)
    lines = wrap(draw, "x" * 200, f, 300, 3)
    assert all(draw.textlength(line, font=f) <= 300 for line in lines)


def test_clock_time_is_twelve_hour_without_suffix():
    assert clock_time(datetime(2026, 11, 8, 21, 46)) == "9:46"
    assert clock_time(datetime(2026, 11, 8, 9, 14)) == "9:14"


def test_clock_time_zero_pads_minutes_only():
    assert clock_time(datetime(2026, 11, 8, 13, 5)) == "1:05"


def test_clock_time_shows_twelve_at_noon_and_midnight():
    assert clock_time(datetime(2026, 11, 8, 12, 0)) == "12:00"
    assert clock_time(datetime(2026, 11, 8, 0, 30)) == "12:30"
