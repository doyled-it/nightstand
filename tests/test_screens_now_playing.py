from __future__ import annotations

from datetime import datetime, timedelta, timezone

from PIL import Image, ImageChops, ImageDraw

from nightstand.models import NowPlaying
from nightstand.palette import PURE
from nightstand.screens import now_playing

# The now-playing screen carries much more large, bold text than the art and
# placeholder screens, so arm64-vs-x86_64 glyph-edge noise is higher (measured at
# about 1.9%). Allow more headroom than the default golden tolerance while staying
# well below any real layout change.
GLYPH_HEAVY_TOLERANCE = 0.03

PST = timezone(timedelta(hours=-8))
NOW = datetime(2026, 11, 8, 21, 14, 3, tzinfo=PST)


def _is_six_color(img: Image.Image) -> bool:
    colors = img.convert("RGB").getcolors(maxcolors=256)
    if colors is None:
        return False
    return {color for _, color in colors} <= set(PURE.values())


def sample_cover() -> Image.Image:
    """A deterministic non-square cover image, built in code (nothing downloaded)."""
    img = Image.new("RGB", (640, 480))
    draw = ImageDraw.Draw(img)
    for y in range(480):
        draw.line([(0, y), (640, y)], fill=(40, y * 255 // 480, 180 - y * 120 // 480))
    draw.ellipse([180, 120, 460, 400], fill=(240, 210, 60))
    draw.rectangle([60, 60, 160, 420], fill=(200, 40, 50))
    return img


def a_music_track(title: str = "Tidal Drift") -> NowPlaying:
    return NowPlaying(
        title=title,
        artist="Nova Reef, The Saltwater Choir",
        album="Blue Hour",
        art_url="http://hub.local/cover/blue-hour.jpg",
        rooms=("Living room", "Bedroom"),
        kind="music",
        duration=322.0,
        started_at=NOW - timedelta(seconds=100),
    )


def a_podcast() -> NowPlaying:
    return NowPlaying(
        title="[AUDIO] Example Show Bonus #58 - Guest One and Guest Two",
        artist="Example Show - Patreon Feed",
        album="Example Show - Patreon Feed",
        art_url="http://hub.local/cover/show.jpg",
        rooms=("Bedroom",),
        kind="long_form",
        status="playing",
        duration=5845.0,
        position=3605.0,
        position_at=NOW - timedelta(seconds=5),
        started_at=NOW - timedelta(seconds=3605),
    )


def a_no_duration_track() -> NowPlaying:
    return NowPlaying(
        title="Live on the air",
        artist="Internet Radio",
        album=None,
        rooms=("Kitchen",),
        kind="music",
    )


# --- Goldens -----------------------------------------------------------------


def test_music_with_cover_matches_golden(assert_golden):
    rendered = now_playing.render(a_music_track(), sample_cover(), NOW)
    assert_golden(rendered, "now_playing_music_cover.png", GLYPH_HEAVY_TOLERANCE)


def test_podcast_with_cover_and_bar_matches_golden(assert_golden):
    rendered = now_playing.render(a_podcast(), sample_cover(), NOW)
    assert_golden(rendered, "now_playing_podcast_cover.png", GLYPH_HEAVY_TOLERANCE)


def test_podcast_without_cover_matches_golden(assert_golden):
    rendered = now_playing.render(a_podcast(), None, NOW)
    assert_golden(rendered, "now_playing_podcast_text.png", GLYPH_HEAVY_TOLERANCE)


def test_no_duration_track_matches_golden(assert_golden):
    rendered = now_playing.render(a_no_duration_track(), sample_cover(), NOW)
    assert_golden(rendered, "now_playing_no_duration.png", GLYPH_HEAVY_TOLERANCE)


# --- Basic invariants --------------------------------------------------------


def test_render_is_panel_size():
    assert now_playing.render(a_music_track(), sample_cover(), NOW).size == (800, 480)


def test_render_with_cover_is_six_color():
    assert _is_six_color(now_playing.render(a_podcast(), sample_cover(), NOW))


def test_render_without_cover_is_six_color():
    assert _is_six_color(now_playing.render(a_podcast(), None, NOW))


# --- Title shrink and ellipsis ----------------------------------------------


def _draw() -> ImageDraw.ImageDraw:
    return ImageDraw.Draw(Image.new("RGB", (800, 480)))


def test_long_natural_title_fits_four_lines_above_minimum():
    # A natural title of around ninety characters must fit in four lines without
    # dropping to the smallest size or being ellipsized.
    title = (
        "Letters From A Small Island On The Edge Of A Very Large "
        "And Restless Northern Grey Sea"
    )
    assert len(title) >= 85
    max_width = now_playing.TEXT_RIGHT - now_playing.TEXT_X_WITH_COVER
    fnt, lines = now_playing.fit_title(_draw(), title, max_width)
    assert len(lines) <= 4
    assert not any(line.endswith("…") for line in lines)
    assert fnt.size > 28


def test_only_ellipsizes_at_the_smallest_size():
    title = "Movement " * 40  # 360 characters: far too long for four lines
    max_width = now_playing.TEXT_RIGHT - now_playing.TEXT_X_WITH_COVER
    fnt, lines = now_playing.fit_title(_draw(), title.strip(), max_width)
    assert fnt.size == 28
    assert lines[-1].endswith("…")


# --- Bounds (Review Focus 5) -------------------------------------------------


def a_text_only(title: str) -> NowPlaying:
    """A track that draws only the text column: no duration (no strip), no rooms.

    Isolating the text column is what lets the bounds tests bite. The strip and
    chips are drawn at fixed y values at or below ``STRIP_TOP`` by construction,
    so the one thing that could cross into them is the title/artist/album block;
    with the strip and chips absent, any non-white pixel below ``STRIP_TOP`` (or
    right of ``TEXT_RIGHT``) is the text column overflowing, which the crop below
    is wide and tall enough to catch.
    """
    return NowPlaying(
        title=title,
        artist="Nova Reef, The Saltwater Choir",
        album="Blue Hour",
        art_url="http://hub.local/cover/x.jpg",
        rooms=(),
    )


def _column_bbox(img: Image.Image, left: int):
    """Bbox of non-white pixels anywhere from ``left`` to the right/bottom edges.

    The crop runs to the full width and height of the canvas (not to the asserted
    limits), so a title that overflowed past ``TEXT_RIGHT`` or down into the strip
    produces a bbox that exceeds the limit instead of being silently clipped to
    it. Returns absolute image coordinates.
    """
    region = img.crop((left, 0, now_playing.WIDTH, now_playing.HEIGHT))
    white = Image.new("RGB", region.size, PURE["white"])
    bbox = ImageChops.difference(region, white).getbbox()
    if bbox is None:
        return None
    return (bbox[0] + left, bbox[1], bbox[2] + left, bbox[3])


def test_long_title_stays_inside_the_text_box():
    np = a_text_only(title="Movement " * 40)  # 360 characters
    rendered = now_playing.render(np, sample_cover(), NOW).convert("RGB")
    bbox = _column_bbox(rendered, now_playing.TEXT_X_WITH_COVER)
    assert bbox is not None, "expected the text column to draw something"
    assert bbox[2] <= now_playing.TEXT_RIGHT, f"title reached x={bbox[2]}"
    assert bbox[3] <= now_playing.STRIP_TOP, (
        f"title reached y={bbox[3]}, into the strip at y={now_playing.STRIP_TOP}"
    )


def test_cjk_title_stays_inside_the_text_box():
    # Inter has no CJK glyphs; the title must still shrink and stay in its box.
    np = a_text_only(title="わたしはあなたのことをずっとまっています" * 6)
    rendered = now_playing.render(np, sample_cover(), NOW).convert("RGB")
    bbox = _column_bbox(rendered, now_playing.TEXT_X_WITH_COVER)
    if bbox is not None:
        assert bbox[2] <= now_playing.TEXT_RIGHT
        assert bbox[3] <= now_playing.STRIP_TOP


def test_full_width_long_title_without_cover_clears_the_strip():
    np = a_text_only(title="Movement " * 40)
    rendered = now_playing.render(np, None, NOW).convert("RGB")
    bbox = _column_bbox(rendered, now_playing.TEXT_X_NO_COVER)
    assert bbox is not None
    assert bbox[2] <= now_playing.TEXT_RIGHT
    assert bbox[3] <= now_playing.STRIP_TOP


def test_maximal_block_with_bar_does_not_overlap_the_strip():
    # A title that fills four lines at the largest size, with artist and album:
    # the worst-case block height. The long-form track draws the bar (asserted
    # separately), and the same title rendered without the strip and chips lets
    # the bounds crop measure the block in isolation.
    title = "Winter Tides Roll In Across The Cold Grey Bay At Dusk"
    fnt, lines = now_playing.fit_title(
        _draw(), title, now_playing.TEXT_RIGHT - now_playing.TEXT_X_WITH_COVER
    )
    assert fnt.size == 52 and len(lines) == 4, "fixture should hit the worst case"

    long_form = NowPlaying(
        title=title,
        artist="The Tidewater Collective Ensemble",
        album="Anthology Of Distant Harbours Deluxe",
        art_url="http://hub.local/cover/x.jpg",
        rooms=("Bedroom",),
        kind="long_form",
        duration=5845.0,
        position=3605.0,
        position_at=NOW,
        started_at=NOW - timedelta(seconds=3605),
    )
    with_bar = now_playing.render(long_form, sample_cover(), NOW).convert("RGB")
    bar_colors = {c for _, c in _strip_region(with_bar).getcolors(maxcolors=256)}
    assert PURE["red"] in bar_colors, "the long-form track should draw its bar"

    text_only = NowPlaying(
        title=title,
        artist="The Tidewater Collective Ensemble",
        album="Anthology Of Distant Harbours Deluxe",
        art_url="http://hub.local/cover/x.jpg",
        rooms=(),
    )
    rendered = now_playing.render(text_only, sample_cover(), NOW).convert("RGB")
    bbox = _column_bbox(rendered, now_playing.TEXT_X_WITH_COVER)
    assert bbox is not None
    assert bbox[3] <= now_playing.STRIP_TOP


# --- No artist, no album (Review Focus 2) -----------------------------------


def test_title_only_track_draws_no_empty_lines():
    # A title but no artist and no album: the title sits where it would with an
    # artist and album present, with nothing drawn in their rows.
    # No rooms, so the chip row does not mask where the text column stops.
    bare = NowPlaying(title="Static Fields", artist="", album=None, rooms=())
    full = NowPlaying(
        title="Static Fields",
        artist="Someone",
        album="Something",
        rooms=(),
    )
    bare_img = now_playing.render(bare, None, NOW).convert("RGB")
    full_img = now_playing.render(full, None, NOW).convert("RGB")

    # The title occupies the same rows in both; only the artist/album rows below
    # it differ. Measure where drawing stops in each: the bare track stops higher.
    bare_bbox = _column_bbox(bare_img, now_playing.TEXT_X_NO_COVER)
    full_bbox = _column_bbox(full_img, now_playing.TEXT_X_NO_COVER)
    assert bare_bbox is not None and full_bbox is not None
    assert bare_bbox[3] < full_bbox[3], "artist/album rows should add height"
    # The titles start at the same y: no blank line was pushed in above them.
    assert bare_bbox[1] == full_bbox[1]


# --- End-time math -----------------------------------------------------------


def test_end_time_from_position_extrapolates_remaining():
    np = a_podcast()  # duration 5845, position 3605 at NOW - 5s, playing
    end = now_playing.end_time(np, NOW)
    assert end is not None
    # position_now at NOW = 3610; remaining = 5845 - 3610 = 2235 s.
    assert end == NOW + timedelta(seconds=5845 - 3610)


def test_end_time_without_position_uses_started_at_plus_duration():
    np = NowPlaying(
        title="Song",
        artist="Artist",
        album="Album",
        rooms=("Bedroom",),
        kind="music",
        duration=322.0,
        started_at=NOW - timedelta(seconds=40),
    )
    end = now_playing.end_time(np, NOW)
    assert end == np.started_at + timedelta(seconds=322.0)


def test_end_time_is_none_without_duration():
    assert now_playing.end_time(a_no_duration_track(), NOW) is None


# --- Progress line format ----------------------------------------------------


def test_hms_formats_hour_plus_as_hmmss_and_shorter_as_mmss():
    assert now_playing._hms(3610, hours=True) == "1:00:10"
    assert now_playing._hms(5845, hours=True) == "1:37:25"
    assert now_playing._hms(322, hours=False) == "5:22"
    assert now_playing._hms(1390, hours=False) == "23:10"


def test_progress_text_uses_hours_for_a_long_podcast():
    # duration 5845 s (>= 1 h), position 3605 at NOW - 5 s while playing -> 3610.
    assert now_playing.progress_text(a_podcast(), NOW).startswith(
        "1:00:10 of 1:37:25"
    )


def test_progress_text_decides_format_by_the_total_not_the_position():
    # A long-form track past an hour with the position still under an hour: both
    # numbers take the h:mm:ss form because the total (the duration) decides.
    np = NowPlaying(
        title="Long Mix",
        artist="DJ",
        album="Set",
        rooms=("Bedroom",),
        kind="long_form",
        duration=5400.0,
        position=600.0,
        position_at=NOW,
        started_at=NOW - timedelta(seconds=600),
    )
    assert now_playing.progress_text(np, NOW).startswith("0:10:00 of 1:30:00")


def test_progress_text_music_stays_in_minutes_below_an_hour():
    assert now_playing.progress_text(a_music_track(), NOW).startswith("5:22")


def test_progress_text_is_none_without_duration():
    assert now_playing.progress_text(a_no_duration_track(), NOW) is None


# --- Strip contents ----------------------------------------------------------


def _strip_region(img: Image.Image) -> Image.Image:
    return img.crop(
        (0, now_playing.STRIP_TOP, now_playing.TEXT_RIGHT, now_playing.PROGRESS_TOP)
    )


def test_long_form_draws_a_red_progress_bar():
    rendered = now_playing.render(a_podcast(), sample_cover(), NOW).convert("RGB")
    colors = {color for _, color in _strip_region(rendered).getcolors(maxcolors=256)}
    assert PURE["red"] in colors


def test_music_draws_no_progress_bar():
    rendered = now_playing.render(a_music_track(), sample_cover(), NOW).convert("RGB")
    colors = {color for _, color in _strip_region(rendered).getcolors(maxcolors=256)}
    assert PURE["red"] not in colors


def test_no_duration_draws_no_strip_text():
    # Below the bar region and above the chips, a no-duration track is blank.
    rendered = now_playing.render(
        a_no_duration_track(), sample_cover(), NOW
    ).convert("RGB")
    line = rendered.crop(
        (
            now_playing.MARGIN,
            now_playing.PROGRESS_TOP,
            now_playing.TEXT_RIGHT,
            now_playing.PROGRESS_TOP + 34,
        )
    )
    white = Image.new("RGB", line.size, PURE["white"])
    assert ImageChops.difference(line, white).getbbox() is None


def test_rooms_are_drawn_as_chips():
    rendered = now_playing.render(a_music_track(), sample_cover(), NOW).convert("RGB")
    # The chip row is black blocks near the bottom-left; expect black pixels there.
    chips = rendered.crop((now_playing.MARGIN, now_playing.STRIP_TOP, 400, 480))
    colors = {color for _, color in chips.getcolors(maxcolors=256)}
    assert PURE["black"] in colors
