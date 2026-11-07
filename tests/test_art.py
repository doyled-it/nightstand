from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from PIL import Image

from nightstand.art import ArtLibrary, art_slot, load_piece_image
from nightstand.models import ArtPiece

NOW = datetime(2026, 11, 6, 12, 0, 0)


def test_art_slot_steps_once_per_rotation():
    rotate = 60
    start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    base = art_slot(start, rotate)
    assert art_slot(start + timedelta(minutes=59), rotate) == base
    assert art_slot(start + timedelta(minutes=60), rotate) == base + 1


def _write_image(path: Path, color: tuple[int, int, int] = (10, 20, 30)) -> None:
    Image.new("RGB", (64, 64), color).save(path)


def test_caption_from_sidecar(tmp_path):
    _write_image(tmp_path / "coastal.jpg")
    (tmp_path / "coastal.jpg.txt").write_text("Coastal Study\nArtist Name\n")

    piece = ArtLibrary(tmp_path).pieces()[0]

    assert piece.title == "Coastal Study"
    assert piece.artist == "Artist Name"


def test_caption_title_only_sidecar(tmp_path):
    _write_image(tmp_path / "study.png")
    (tmp_path / "study.png.txt").write_text("Just A Title\n")

    piece = ArtLibrary(tmp_path).pieces()[0]

    assert piece.title == "Just A Title"
    assert piece.artist is None


def test_sidecar_source_url_when_https(tmp_path):
    _write_image(tmp_path / "coastal.jpg")
    (tmp_path / "coastal.jpg.txt").write_text(
        "Coastal Study\nArtist Name\nhttps://example.org/art/123\n"
    )

    piece = ArtLibrary(tmp_path).pieces()[0]

    assert piece.title == "Coastal Study"
    assert piece.artist == "Artist Name"
    assert piece.source_url == "https://example.org/art/123"


def test_sidecar_source_url_ignored_when_not_https(tmp_path):
    _write_image(tmp_path / "coastal.jpg")
    (tmp_path / "coastal.jpg.txt").write_text(
        "Coastal Study\nArtist Name\nnot a url\n"
    )

    piece = ArtLibrary(tmp_path).pieces()[0]

    assert piece.source_url is None


def test_sidecar_source_url_rejects_non_https_scheme(tmp_path):
    _write_image(tmp_path / "coastal.jpg")
    (tmp_path / "coastal.jpg.txt").write_text(
        "Coastal Study\nArtist Name\nhttp://example.org/art/123\n"
    )

    piece = ArtLibrary(tmp_path).pieces()[0]

    assert piece.source_url is None


def test_sidecar_source_url_with_empty_artist(tmp_path):
    # Line 2 blank, line 3 a URL: the artist is None and the URL still parses.
    _write_image(tmp_path / "coastal.jpg")
    (tmp_path / "coastal.jpg.txt").write_text(
        "Coastal Study\n\nhttps://example.org/art/123\n"
    )

    piece = ArtLibrary(tmp_path).pieces()[0]

    assert piece.title == "Coastal Study"
    assert piece.artist is None
    assert piece.source_url == "https://example.org/art/123"


def test_two_line_sidecar_has_no_source_url(tmp_path):
    _write_image(tmp_path / "coastal.jpg")
    (tmp_path / "coastal.jpg.txt").write_text("Coastal Study\nArtist Name\n")

    piece = ArtLibrary(tmp_path).pieces()[0]

    assert piece.source_url is None


def test_caption_from_filename(tmp_path):
    _write_image(tmp_path / "coastal_study-1.jpg")

    piece = ArtLibrary(tmp_path).pieces()[0]

    assert piece.title == "coastal study 1"
    assert piece.artist is None


def test_pieces_filtered_and_sorted(tmp_path):
    _write_image(tmp_path / "b.png")
    _write_image(tmp_path / "a.jpg")
    _write_image(tmp_path / "c.webp")
    (tmp_path / "notes.txt").write_text("ignore me")
    (tmp_path / "skip.gif").write_bytes(b"not listed")

    names = [p.path.name for p in ArtLibrary(tmp_path).pieces()]

    assert names == ["a.jpg", "b.png", "c.webp"]


def test_pick_stable_within_slot_and_changes_across_slots(tmp_path):
    for i in range(6):
        _write_image(tmp_path / f"art_{i}.png")
    lib = ArtLibrary(tmp_path)

    first = lib.pick(NOW, 60)
    same_slot = lib.pick(NOW + timedelta(minutes=1), 60)
    next_slot = lib.pick(NOW + timedelta(minutes=60), 60)

    assert first == same_slot
    assert first != next_slot


def test_corrupt_image_skipped(tmp_path):
    bad = tmp_path / "bad.jpg"
    bad.write_bytes(b"nope")

    assert load_piece_image(ArtPiece(path=bad, title="Bad", artist=None)) is None


def test_load_piece_image_reads_real_file(tmp_path):
    good = tmp_path / "good.png"
    _write_image(good)

    image = load_piece_image(ArtPiece(path=good, title="Good", artist=None))

    assert image is not None
    assert image.size == (64, 64)


def test_empty_library_pick_is_none(tmp_path):
    assert ArtLibrary(tmp_path).pick(NOW, 60) is None


def test_pick_skips_corrupt_piece_for_every_slot(tmp_path):
    # One corrupt file among several good ones. pick must walk the slot's
    # shuffled order past the corrupt file and return a readable piece for
    # every slot of the day, never the broken one.
    for i in range(5):
        _write_image(tmp_path / f"good_{i}.png")
    bad = tmp_path / "zbad.jpg"
    bad.write_bytes(b"nope")
    lib = ArtLibrary(tmp_path)

    for slot in range(48):
        now = NOW + timedelta(minutes=60 * slot)
        piece = lib.pick(now, 60)
        assert piece is not None
        assert piece.path != bad
        assert load_piece_image(piece) is not None


def test_pick_returns_none_when_every_piece_is_corrupt(tmp_path):
    for i in range(3):
        (tmp_path / f"bad_{i}.jpg").write_bytes(b"nope")

    assert ArtLibrary(tmp_path).pick(NOW, 60) is None
